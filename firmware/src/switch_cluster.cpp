/*
 * The button, reported as a button.
 *
 * WHAT THIS DOES NOT CHANGE. The switch already commands its bulbs directly
 * over Thread, through the binding table, and that keeps working with the Pi
 * unplugged. This cluster sends nothing to a bulb and decides nothing about
 * behaviour: toggle on a short press and full brightness on a long one still
 * live in automation.cpp. Matter's Switch cluster is a microphone, not a wire.
 *
 * WHY IT WAS NEEDED. The panel could not tell that a wall switch had been
 * touched. It has known how to read presses since the IKEA remote arrived -
 * gestures, short, long, double, all of it - and our own switches simply never
 * spoke: endpoint 1 carries Binding, Descriptor, Groups and Identify, and no
 * Switch cluster at all. So a long press set a lamp to full and the schedule,
 * knowing nothing, wrote the curve's brightness back on its next tick. The
 * light you asked for lasted seconds, and nothing anywhere was wrong.
 *
 * TWO POSITIONS IS ONE BUTTON. NumberOfPositions is 2 because a momentary
 * button HAS two positions - released and pressed. A remote with two buttons
 * carries two endpoints, one each; this is one button, so it is one endpoint
 * with two positions.
 *
 * WHY A DYNAMIC ENDPOINT. Putting the cluster on endpoint 1 means regenerating
 * the data model from the SDK. lock_cluster.cpp declined that trade for its
 * vendor cluster and battery.cpp declined it for PowerSource; this is the third
 * time, and the answer has not changed. Declared in C++, served through an
 * AttributeAccessInterface, and Generic Switch is a device type in its own
 * right - so an endpoint of its own is how Matter would model it anyway.
 *
 * WHAT THE PANEL DOES WITH IT. Nothing new: matter_event() and gesture_of()
 * were written for the remote and work unchanged. A long press is what the
 * panel holds the light for - see remote_act().
 */
#include "switch_cluster.h"

#include <app-common/zap-generated/ids/Attributes.h>
#include <app-common/zap-generated/ids/Clusters.h>
#include <app/AttributeAccessInterface.h>
#include <app/AttributeAccessInterfaceRegistry.h>
#include <app/EventLogging.h>
#include <app/reporting/reporting.h>
#include <app/util/attribute-storage.h>
#include <platform/CHIPDeviceLayer.h>

#include <zephyr/logging/log.h>

LOG_MODULE_DECLARE(app, CONFIG_SMARTHOME_LOG_LEVEL);

using namespace chip;
using namespace chip::app;
using namespace chip::app::Clusters;

namespace {

/* 1 is the switch, 2 the vendor cluster, 3 the battery - so the button takes 4,
 * and dynamic index 2 after those two. Endpoint numbers are read once, at
 * commissioning, and are not free to renumber afterwards. */
constexpr EndpointId kSwitchEndpoint = 4;
constexpr uint16_t kDynamicIndex = 2;
constexpr chip::DeviceTypeId kGenericSwitchDeviceType = 0x000F;
constexpr uint16_t kClusterRevision = 1;

/* MS | MSR | MSL - momentary, reports releases, detects a long press. Not MSM:
 * the firmware does not count double taps, and claiming a feature we do not
 * implement would have controllers waiting for events that never come. */
constexpr uint32_t kFeatureMap = (1u << 1) | (1u << 2) | (1u << 3);

constexpr uint8_t kPositions = 2;
constexpr uint8_t kReleased = 0;
constexpr uint8_t kPressed = 1;

uint8_t sPosition = kReleased;

DECLARE_DYNAMIC_ATTRIBUTE_LIST_BEGIN(sSwitchAttrs)
DECLARE_DYNAMIC_ATTRIBUTE(Switch::Attributes::NumberOfPositions::Id, INT8U, 1, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(Switch::Attributes::CurrentPosition::Id, INT8U, 1, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(Switch::Attributes::FeatureMap::Id, BITMAP32, 4, 0),
	DECLARE_DYNAMIC_ATTRIBUTE_LIST_END();

/* Descriptor is mandatory on every endpoint; the SDK answers for it itself. */
DECLARE_DYNAMIC_ATTRIBUTE_LIST_BEGIN(sDescriptorAttrs)
DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::DeviceTypeList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::ServerList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::ClientList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::PartsList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE_LIST_END();

DECLARE_DYNAMIC_CLUSTER_LIST_BEGIN(sClusters)
DECLARE_DYNAMIC_CLUSTER(Switch::Id, sSwitchAttrs, ZAP_CLUSTER_MASK(SERVER), nullptr, nullptr),
	DECLARE_DYNAMIC_CLUSTER(Descriptor::Id, sDescriptorAttrs, ZAP_CLUSTER_MASK(SERVER), nullptr,
				nullptr) DECLARE_DYNAMIC_CLUSTER_LIST_END;

DECLARE_DYNAMIC_ENDPOINT(sEndpoint, sClusters);
DataVersion sDataVersions[ArraySize(sClusters)];
const EmberAfDeviceType sDeviceTypes[] = { { kGenericSwitchDeviceType, 1 } };

class SwitchAttrAccess : public AttributeAccessInterface {
public:
	SwitchAttrAccess() : AttributeAccessInterface(MakeOptional(kSwitchEndpoint), Switch::Id) {}

	CHIP_ERROR Read(const ConcreteReadAttributePath &path, AttributeValueEncoder &encoder) override
	{
		switch (path.mAttributeId) {
		case Switch::Attributes::NumberOfPositions::Id:
			return encoder.Encode(kPositions);
		case Switch::Attributes::CurrentPosition::Id:
			return encoder.Encode(sPosition);
		case Switch::Attributes::FeatureMap::Id:
			return encoder.Encode(kFeatureMap);
		case Switch::Attributes::ClusterRevision::Id:
			return encoder.Encode(kClusterRevision);
		default:
			return CHIP_IM_GLOBAL_STATUS(UnsupportedAttribute);
		}
	}
};

SwitchAttrAccess sAttrAccess;

void SetPosition(uint8_t pos)
{
	if (sPosition == pos) {
		return;
	}
	sPosition = pos;
	MatterReportingAttributeChangeCallback(kSwitchEndpoint, Switch::Id,
					       Switch::Attributes::CurrentPosition::Id);
}

/* A failed event is worth a line and nothing more. The bulb has already been
 * commanded by the time any of this runs, so the light is right either way -
 * what is lost is the panel knowing about it, which is not worth a reset. */
template <typename T> void Emit(const T &event, const char *what)
{
	EventNumber number = 0;
	CHIP_ERROR err = LogEvent(event, kSwitchEndpoint, number);
	if (err != CHIP_NO_ERROR) {
		LOG_WRN("switch: %s not reported: %" CHIP_ERROR_FORMAT, what, err.Format());
	}
}

} /* namespace */

namespace SwitchCluster {

void Init(void)
{
	CHIP_ERROR err = emberAfSetDynamicEndpoint(kDynamicIndex, kSwitchEndpoint, &sEndpoint,
						   Span<DataVersion>(sDataVersions),
						   Span<const EmberAfDeviceType>(sDeviceTypes));
	if (err != CHIP_NO_ERROR) {
		LOG_ERR("switch: cannot add the endpoint: %s", ErrorStr(err));
		return;
	}
	AttributeAccessInterfaceRegistry::Instance().Register(&sAttrAccess);
}

void Pressed(void)
{
	SetPosition(kPressed);
	Switch::Events::InitialPress::Type event;
	event.newPosition = kPressed;
	Emit(event, "press");
}

void LongHeld(void)
{
	Switch::Events::LongPress::Type event;
	event.newPosition = kPressed;
	Emit(event, "long press");
}

void Released(bool wasLong)
{
	SetPosition(kReleased);
	if (wasLong) {
		Switch::Events::LongRelease::Type event;
		event.previousPosition = kPressed;
		Emit(event, "long release");
	} else {
		Switch::Events::ShortRelease::Type event;
		event.previousPosition = kPressed;
		Emit(event, "short release");
	}
}

} /* namespace SwitchCluster */
