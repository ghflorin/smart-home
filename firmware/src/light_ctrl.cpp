/*
 * Implementation modeled on nrf/samples/matter/common/src/binding/binding_handler.cpp.
 *
 * Why we do not use Nrf::Matter::BindingHandler directly: its BindingData struct
 * carries a single payload field (uint8_t Value), which cannot hold
 * level + transitionTime, and it has no path for WriteAttribute (only Invoke).
 * We do keep its CASE session recovery logic on timeout.
 *
 * NOTE: do not also call Nrf::Matter::BindingHandler::Init() - the two would
 * overwrite each other's handlers on BindingManager.
 */
#include "light_ctrl.h"

#include <app/server/Server.h>
#include <app/util/binding-table.h>
#include <controller/InvokeInteraction.h>
#include <controller/WriteInteraction.h>
#include <lib/support/CHIPMem.h>
#include <lib/support/CodeUtils.h>
#include <platform/CHIPDeviceLayer.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(light_ctrl, CONFIG_SMARTHOME_LOG_LEVEL);

using namespace chip;
using namespace chip::app;

namespace {

EndpointId sSwitchEndpoint = 1;

/* The context of one request. Allocated with Platform::New, released in the
 * success/failure callback, or by BindingManager via ContextReleaseHandler. */
struct BindingCtx {
	LightCtrl::Request req;
	bool caseSessionRecovered;
};

/* Which cluster the binding notification goes out on. It has to match the table
 * entries written by chip-tool (cluster 6 = OnOff, 8 = LevelControl), otherwise
 * BindingManager finds no matching entry. */
/* Our own cluster, on endpoint 2. Must be the same value as in lock_cluster.cpp
 * and as SCHED_CLUSTER in panel/server.py. */
constexpr ClusterId kSmartHomeCluster = 0xFFF1FC30;
constexpr AttributeId kLockedAttr = 0xFFF10002;

/* Hand-written TypeInfo, because this cluster does not go through ZAP and so
 * has no generated accessor. This is all Controller::WriteAttribute needs. */
struct LockedAttrTypeInfo {
	using Type = bool;
	using DecodableType = bool;
	using DecodableArgType = bool;
	static constexpr ClusterId GetClusterId() { return kSmartHomeCluster; }
	static constexpr AttributeId GetAttributeId() { return kLockedAttr; }
	static constexpr bool MustUseTimedWrite() { return false; }
};

ClusterId ClusterFor(LightCtrl::Action action)
{
	switch (action) {
	case LightCtrl::Action::WriteLock:
		return kSmartHomeCluster;
	case LightCtrl::Action::SetLevel:
	case LightCtrl::Action::WriteStartupLevel:
		return Clusters::LevelControl::Id;
	case LightCtrl::Action::SetColorTemp:
		return Clusters::ColorControl::Id;
	default:
		return Clusters::OnOff::Id;
	}
}

void ReleaseCtx(BindingCtx *ctx)
{
	Platform::Delete<BindingCtx>(ctx);
}

void HandleFailure(BindingCtx *ctx, CHIP_ERROR error)
{
	VerifyOrReturn(ctx != nullptr);

	if (error == CHIP_ERROR_TIMEOUT && !ctx->caseSessionRecovered) {
		LOG_INF("timeout, attempting CASE session recovery");
		ctx->caseSessionRecovered = true;

		CHIP_ERROR err = BindingManager::GetInstance().NotifyBoundClusterChanged(
			sSwitchEndpoint, ClusterFor(ctx->req.action), static_cast<void *>(ctx));
		if (err != CHIP_NO_ERROR) {
			LOG_ERR("retry failed: %" CHIP_ERROR_FORMAT, err.Format());
			ReleaseCtx(ctx);
		}
	} else {
		/* Usual causes: the bulb is offline, the ACL is too narrow
		 * (Manage missing for the startup state write - that returns
		 * UNSUPPORTED_ACCESS), or the binding points at a node ID that no
		 * longer exists. */
		LOG_ERR("command failed: %" CHIP_ERROR_FORMAT, error.Format());
		ReleaseCtx(ctx);
	}
}

/* WHO OWNS THE CONTEXT, and why we work on a copy.
 *
 * The context handed to NotifyBoundClusterChanged belongs to BindingManager,
 * not to us: it calls IncrementConsumersNumber on entry and
 * DecrementConsumersNumber at the 'exit' label (BindingManager.cpp), and once
 * the count hits zero it invokes the release handler itself - that is
 * DeviceContextReleaseHandler below. This happens microseconds after the
 * command goes out, long before the bulb's response comes back.
 *
 * If the response lambdas captured THE SAME pointer, they would free it a
 * second time when the response arrived: a double free over already-recycled
 * memory, with assertions compiled out of this build, so without a trace.
 *
 * So each helper makes its OWN COPY of the context and owns that. Exactly one
 * of the two lambdas runs, so the copy is freed exactly once and never leaks.
 * The original stays BindingManager's, to release however it sees fit.
 *
 * This stayed invisible until now: no response ever arrived, because the
 * binding pointed at a bulb that did not exist.
 *
 * Invoke and Write both take std::function callbacks, but with different
 * signatures (Invoke gets path + status, Write only path), so they need two
 * separate helpers. */
template <typename CommandType>
void Invoke(Messaging::ExchangeManager *exchangeMgr, const SessionHandle &session,
	    EndpointId remoteEp, const CommandType &cmd, BindingCtx *ctx)
{
	auto *own = Platform::New<BindingCtx>(*ctx);
	VerifyOrReturn(own != nullptr, LOG_ERR("out of memory for the command context"));

	CHIP_ERROR err = Controller::InvokeCommandRequest(
		exchangeMgr, session, remoteEp, cmd,
		[own](const ConcreteCommandPath &, const StatusIB &,
		      const typename CommandType::ResponseType &) {
			LOG_DBG("command acked by bulb");
			ReleaseCtx(own);
		},
		[own](CHIP_ERROR error) { HandleFailure(own, error); });

	if (err != CHIP_NO_ERROR) {
		HandleFailure(own, err);
	}
}

/* Controller::WriteAttribute takes a SessionHandle, not an ExchangeManager. */
template <typename AttrTypeInfo, typename ValueType>
void WriteAttr(const SessionHandle &session, EndpointId remoteEp, const ValueType &value,
	       BindingCtx *ctx)
{
	auto *own = Platform::New<BindingCtx>(*ctx);
	VerifyOrReturn(own != nullptr, LOG_ERR("out of memory for the write context"));

	CHIP_ERROR err = Controller::WriteAttribute<AttrTypeInfo>(
		session, remoteEp, value,
		[own](const ConcreteAttributePath &) {
			LOG_INF("startup state written to bulb");
			ReleaseCtx(own);
		},
		[own](const ConcreteAttributePath *, CHIP_ERROR error) {
			HandleFailure(own, error);
		});

	if (err != CHIP_NO_ERROR) {
		HandleFailure(own, err);
	}
}

/* Called by BindingManager once per entry in the binding table. deviceProxy
 * already holds an established CASE session to the bulb. */
void DeviceChangedCallback(const EmberBindingTableEntry &binding,
			   OperationalDeviceProxy *deviceProxy, void *context)
{
	auto *ctx = static_cast<BindingCtx *>(context);
	VerifyOrReturn(ctx != nullptr, LOG_ERR("invalid context"));

	/* Groupcast is of no use here: we want a per-bulb acknowledgement and
	 * attribute writes, neither of which exists over multicast. */
	VerifyOrReturn(binding.type == MATTER_UNICAST_BINDING);
	VerifyOrReturn(deviceProxy != nullptr && deviceProxy->ConnectionReady(),
		       LOG_WRN("no session available to the bulb"));

	auto *exchangeMgr = deviceProxy->GetExchangeManager();

	/* GetSecureSession() returns Optional<SessionHandle> BY VALUE
	 * (DeviceProxy.h:54), and Optional::Value() only has '&' and 'const &'
	 * overloads (Optional.h:199,206) - so it hands back a reference INTO the
	 * temporary. Binding a reference to the result of a function call does not
	 * extend that temporary's lifetime: it dies at the end of the full
	 * expression, and the rest of the function would be operating on dead
	 * stack. That stack slot really does get reused a few statements later, so
	 * SendCommandRequest was given a session whose internal pointer was a stack
	 * address - dereferenced and written through, i.e. a guaranteed fault on
	 * every command.
	 *
	 * This stayed invisible until now: DeviceChangedCallback runs ONLY when the
	 * CASE session succeeds, and the binding pointed at a bulb that did not
	 * exist. The first command to a real bulb was also the first fault.
	 *
	 * Holding the Optional in a local keeps the reference valid for the rest of
	 * the function. Copy elision is guaranteed (C++17), so nothing is copied -
	 * SessionHandle has no copy constructor anyway. */
	auto sessionOpt = deviceProxy->GetSecureSession();
	VerifyOrReturn(sessionOpt.HasValue(), LOG_WRN("no session available to the bulb"));
	const SessionHandle &sessionHandle = sessionOpt.Value();

	EndpointId remoteEp = binding.remote;

	switch (ctx->req.action) {
	case LightCtrl::Action::On: {
		Clusters::OnOff::Commands::On::Type cmd;
		Invoke(exchangeMgr, sessionHandle, remoteEp, cmd, ctx);
		break;
	}
	case LightCtrl::Action::Off: {
		Clusters::OnOff::Commands::Off::Type cmd;
		Invoke(exchangeMgr, sessionHandle, remoteEp, cmd, ctx);
		break;
	}
	case LightCtrl::Action::Toggle: {
		Clusters::OnOff::Commands::Toggle::Type cmd;
		Invoke(exchangeMgr, sessionHandle, remoteEp, cmd, ctx);
		break;
	}
	case LightCtrl::Action::SetLevel: {
		/* MoveToLevelWithOnOff = turn on if off AND move to level. With a
		 * separate On + MoveToLevel the bulb would flash at the old
		 * brightness before reaching the new one. */
		Clusters::LevelControl::Commands::MoveToLevelWithOnOff::Type cmd;
		cmd.level = ctx->req.level;
		cmd.transitionTime.SetNonNull(ctx->req.transitionDs);
		cmd.optionsMask.ClearAll();
		cmd.optionsOverride.ClearAll();
		Invoke(exchangeMgr, sessionHandle, remoteEp, cmd, ctx);
		break;
	}
	case LightCtrl::Action::SetColorTemp: {
		Clusters::ColorControl::Commands::MoveToColorTemperature::Type cmd;
		cmd.colorTemperatureMireds = ctx->req.mireds;
		cmd.transitionTime = ctx->req.transitionDs;
		cmd.optionsMask.ClearAll();
		cmd.optionsOverride.ClearAll();
		Invoke(exchangeMgr, sessionHandle, remoteEp, cmd, ctx);
		break;
	}
	case LightCtrl::Action::WriteStartupOnOff: {
		DataModel::Nullable<Clusters::OnOff::StartUpOnOffEnum> value;
		if (ctx->req.startupOnOff == 0xFF) {
			/* null = the bulb returns to its state from before the outage. */
			value.SetNull();
		} else {
			value.SetNonNull(static_cast<Clusters::OnOff::StartUpOnOffEnum>(
				ctx->req.startupOnOff));
		}
		WriteAttr<Clusters::OnOff::Attributes::StartUpOnOff::TypeInfo>(sessionHandle,
									       remoteEp, value, ctx);
		break;
	}
	case LightCtrl::Action::WriteStartupLevel: {
		DataModel::Nullable<uint8_t> value;
		value.SetNonNull(ctx->req.level);
		WriteAttr<Clusters::LevelControl::Attributes::StartUpCurrentLevel::TypeInfo>(
			sessionHandle, remoteEp, value, ctx);
		break;
	}
	case LightCtrl::Action::WriteLock: {
		WriteAttr<LockedAttrTypeInfo>(sessionHandle, remoteEp, ctx->req.locked, ctx);
		break;
	}
	}
}

void DeviceContextReleaseHandler(void *context)
{
	VerifyOrReturn(context != nullptr);
	ReleaseCtx(static_cast<BindingCtx *>(context));
}

/* Runs on the Matter thread, via PlatformMgr().ScheduleWork. */
void BindingWorker(intptr_t context)
{
	auto *ctx = reinterpret_cast<BindingCtx *>(context);
	VerifyOrReturn(ctx != nullptr);

	if (BindingTable::GetInstance().Size() == 0) {
		LOG_WRN("binding table is empty - no bulb configured");
		ReleaseCtx(ctx);
		return;
	}

	CHIP_ERROR err = BindingManager::GetInstance().NotifyBoundClusterChanged(
		sSwitchEndpoint, ClusterFor(ctx->req.action), static_cast<void *>(ctx));
	if (err != CHIP_NO_ERROR) {
		LOG_ERR("NotifyBoundClusterChanged: %" CHIP_ERROR_FORMAT, err.Format());
		/* On error BindingManager will not call ContextReleaseHandler. */
		ReleaseCtx(ctx);
	}
}

} /* namespace */

namespace LightCtrl {

EndpointId SwitchEndpoint(void)
{
	return sSwitchEndpoint;
}

CHIP_ERROR Init(chip::EndpointId switchEndpoint)
{
	auto &server = Server::GetInstance();

	sSwitchEndpoint = switchEndpoint;

	/* Without Init being given the fabric table + CASESessionManager +
	 * storage, BindingManager cannot open sessions to the bulbs and
	 * NotifyBoundClusterChanged does nothing at all. */
	CHIP_ERROR err = BindingManager::GetInstance().Init(
		{ &server.GetFabricTable(), server.GetCASESessionManager(),
		  &server.GetPersistentStorage() });
	if (err != CHIP_NO_ERROR) {
		LOG_ERR("BindingManager::Init: %" CHIP_ERROR_FORMAT, err.Format());
		return err;
	}

	BindingManager::GetInstance().RegisterBoundDeviceChangedHandler(DeviceChangedCallback);
	BindingManager::GetInstance().RegisterBoundDeviceContextReleaseHandler(
		DeviceContextReleaseHandler);

	LOG_INF("binding initialized, %d entries in the table",
		BindingTable::GetInstance().Size());
	return CHIP_NO_ERROR;
}

void Post(const Request &req)
{
	auto *ctx = Platform::New<BindingCtx>();
	if (!ctx) {
		LOG_ERR("context allocation failed");
		return;
	}
	ctx->req = req;
	ctx->caseSessionRecovered = false;

	/* ScheduleWork moves execution onto the Matter thread - BindingManager is
	 * not thread-safe, and Post() is called from Zephyr work queues. */
	DeviceLayer::PlatformMgr().ScheduleWork(BindingWorker, reinterpret_cast<intptr_t>(ctx));
}

void On(void)
{
	Post({ Action::On, 0, 0, 0, 0 });
}

void Off(void)
{
	Post({ Action::Off, 0, 0, 0, 0 });
}

void Toggle(void)
{
	Post({ Action::Toggle, 0, 0, 0, 0 });
}

void SetLevel(uint8_t level, uint16_t transitionDs)
{
	if (level < 1) {
		level = 1;
	}
	if (level > 254) {
		level = 254;
	}
	Post({ Action::SetLevel, level, transitionDs, 0, 0 });
}

void WriteStartupState(uint8_t onOff, uint8_t level)
{
	Post({ Action::WriteStartupOnOff, 0, 0, onOff, 0 });
	Post({ Action::WriteStartupLevel, level, 0, 0, 0 });
}

void WriteLock(bool locked)
{
	Request req{};
	req.action = Action::WriteLock;
	req.locked = locked;
	Post(req);
}

void SetColorTemp(uint16_t mireds, uint16_t transitionDs)
{
	if (mireds == 0) {
		return;
	}
	Post({ Action::SetColorTemp, 0, transitionDs, 0, mireds });
}

} /* namespace LightCtrl */
