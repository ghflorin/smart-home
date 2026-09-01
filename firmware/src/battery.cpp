/*
 * battery - how much is left in the coin cell, published over Matter.
 *
 * WHY NOT THE ADC. The first version of this read NRF_SAADC_AVDD, on the belief
 * that the nRF54L can measure its own supply. It cannot. AVDD on this part is
 * the INTERNAL 0.9 V analogue rail - nrfx says so in one line, and the hardware
 * agreed by reporting 903 mV off a 3 V cell. The nRF54L's SAADC offers exactly
 * two internal inputs, AVDD and DVDD, and both are regulated rails that hold
 * still while the battery drains. There is no VDD input on this SoC. Reaching
 * the cell through the ADC would mean a divider soldered across it, which is a
 * soldering job on switches that are already built and a permanent current draw
 * on a supply measured in milliamp-hours.
 *
 * WHAT DOES SEE THE SUPPLY. The power-fail comparator. POFCON takes a threshold
 * between 1.7 and 2.8 V in 0.1 V steps and POFSTAT says whether VDD sits below
 * it, so walking the thresholds downwards and stopping at the first one VDD
 * still clears brackets the supply to within 100 mV. No external parts, no
 * standing current, and it arrives as a firmware update rather than a rework.
 *
 * WHAT THE NUMBER MEANS. It is a LOWER BOUND, not a measurement: 2700 means "at
 * least 2.7 V", and a fresh cell reads 2800 because that is as high as the
 * comparator looks. That ceiling costs nothing: a lithium coin cell holds close
 * to 3 V for most of its life, so "at least 2.8" is the whole of its good years.
 *
 * WHERE EMPTY IS, AND WHY IT MOVED. It was 2.2 V, the point below which the
 * SoC's radio cannot transmit reliably. That is a fact about the chip and it is
 * the wrong end of the problem. Two cells measured 2.6 V at rest and could not
 * keep their switch attached to Thread at all - the lamp still lit its LED and
 * commanded nothing, because a tired cell holds its voltage until the radio asks
 * for current and then collapses. On the old scale those cells read 66 percent
 * and "ok", and the first warning would have come 100 mV after they were already
 * useless.
 *
 * So empty is 2.6 V: measured, not derived. Three steps is all the resolution a
 * 100 mV comparator has left over a window this narrow, and three honest states
 * beat a smooth number that is wrong:
 *
 *     2800  full, ok         - anything from here up, which is most of the life
 *     2700  half, warning    - the last step before it stops working
 *     2600  empty, critical  - observed to drop off the network
 *
 * BatChargeLevel is what to trust; the percentage exists because that is what
 * interfaces draw.
 *
 * WHY HOURLY. A CR2032 does not move in a minute. The sweep itself is nearly
 * free - a few register writes and at most 1.2 ms of settling - but the switch
 * is budgeted at around 10 uA average, and a reading every hour is already far
 * more often than the answer changes.
 *
 * WHY A DYNAMIC ENDPOINT. PowerSource belongs on the root endpoint, and the
 * root endpoint comes from ZAP. An earlier version wrote to it anyway -
 * `Attributes::BatVoltage::Set(0, ...)` - and every write was refused, silently,
 * because the cluster is not on that endpoint and never was:
 *
 *     ServerList on endpoint 0: 29, 31, 40, 42, 48, 49, 50, 51, 52, 53, 54,
 *                               56, 60, 62, 63, 70        <- no 47
 *
 * Putting it there means regenerating the whole data model from the SDK, which
 * is the same trade lock_cluster.cpp already declined for its vendor cluster.
 * So the battery gets an endpoint of its own, declared in C++, carrying the
 * Power Source device type - which is how Matter models a battery that is not
 * on the root endpoint anyway.
 *
 * The attributes are served through an AttributeAccessInterface rather than the
 * external read callback, because that callback is one function for the whole
 * application - lock_cluster.cpp owns it - and because EndpointList is a list,
 * which TLV encodes properly here and awkwardly there.
 */
#include "battery.h"

#include <app-common/zap-generated/ids/Attributes.h>
#include <app-common/zap-generated/ids/Clusters.h>
#include <app/AttributeAccessInterface.h>
#include <app/AttributeAccessInterfaceRegistry.h>
#include <app/reporting/reporting.h>
#include <app/util/attribute-storage.h>
#include <platform/CHIPDeviceLayer.h>

#include <hal/nrf_regulators.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_DECLARE(app, CONFIG_SMARTHOME_LOG_LEVEL);

using namespace chip;
using namespace chip::app;
using namespace chip::app::Clusters;

namespace {

/* Endpoint 1 is the switch, 2 is the vendor cluster from lock_cluster.cpp, so
 * the battery takes 3 - and dynamic index 1, because the lock holds 0. Both
 * numbers are read at commissioning and baked into the fabric, so they are not
 * free to renumber later. */
constexpr EndpointId kBatteryEndpoint = 3;
constexpr uint16_t kDynamicIndex = 1;
constexpr chip::DeviceTypeId kPowerSourceDeviceType = 0x0011;
constexpr uint16_t kClusterRevision = 2;
constexpr uint32_t kFeatureBattery = 1u << 1; /* BAT */

/* The endpoints this power source actually powers: all of them. */
constexpr EndpointId kPowered[] = { 0, 1, 2, 3 };

/* See "WHERE EMPTY IS" above: these come from two cells that failed at 2.6 V,
 * not from the cell's paper curve or the SoC's transmit floor. */
constexpr int32_t kFullMv = 2800;
constexpr int32_t kEmptyMv = 2600;
/* At or below these. Warning fires one comparator step before empty, which is
 * the only warning this resolution can give - and one step is still days on a
 * cell that has been holding 2.8 V for a year. */
constexpr int32_t kWarningMv = 2700;
constexpr int32_t kCriticalMv = 2600;

/* Matter reports this in HALF percent: 0..200. */
constexpr uint8_t kMaxHalfPercent = 200;

constexpr int64_t kIntervalMs = 60 * 60 * 1000;

/* Highest first: the sweep stops at the first threshold the supply still
 * clears, so a healthy cell costs exactly one comparator setup. */
struct Step {
	nrf_regulators_pof_thr_t thr;
	int32_t mv;
};

constexpr Step kSteps[] = {
	{ NRF_REGULATORS_POF_THR_2V8, 2800 }, { NRF_REGULATORS_POF_THR_2V7, 2700 },
	{ NRF_REGULATORS_POF_THR_2V6, 2600 }, { NRF_REGULATORS_POF_THR_2V5, 2500 },
	{ NRF_REGULATORS_POF_THR_2V4, 2400 }, { NRF_REGULATORS_POF_THR_2V3, 2300 },
	{ NRF_REGULATORS_POF_THR_2V2, 2200 }, { NRF_REGULATORS_POF_THR_2V1, 2100 },
	{ NRF_REGULATORS_POF_THR_2V0, 2000 }, { NRF_REGULATORS_POF_THR_1V9, 1900 },
	{ NRF_REGULATORS_POF_THR_1V8, 1800 }, { NRF_REGULATORS_POF_THR_1V7, 1700 },
};

/* The comparator needs a moment after the threshold changes. The sheet's figure
 * is well under this; the whole sweep is at most 1.2 ms either way, once an
 * hour, so there is nothing to save by trimming it. */
constexpr uint32_t kSettleUs = 100;

k_timer sTimer;
struct k_work sWork;

/* Everything the cluster answers with. Written on the Matter thread by
 * Publish(), read on the Matter thread by the accessor below, so no lock. */
int32_t sLastMv = -1;
uint8_t sHalfPercent = 0;
PowerSource::BatChargeLevelEnum sLevel = PowerSource::BatChargeLevelEnum::kOk;
PowerSource::PowerSourceStatusEnum sStatus = PowerSource::PowerSourceStatusEnum::kUnavailable;

DECLARE_DYNAMIC_ATTRIBUTE_LIST_BEGIN(sPowerAttrs)
DECLARE_DYNAMIC_ATTRIBUTE(PowerSource::Attributes::Status::Id, ENUM8, 1, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(PowerSource::Attributes::Order::Id, INT8U, 1, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(PowerSource::Attributes::Description::Id, CHAR_STRING, 32, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(PowerSource::Attributes::BatVoltage::Id, INT32U, 4,
				  ZAP_ATTRIBUTE_MASK(NULLABLE)),
	DECLARE_DYNAMIC_ATTRIBUTE(PowerSource::Attributes::BatPercentRemaining::Id, INT8U, 1,
				  ZAP_ATTRIBUTE_MASK(NULLABLE)),
	DECLARE_DYNAMIC_ATTRIBUTE(PowerSource::Attributes::BatChargeLevel::Id, ENUM8, 1, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(PowerSource::Attributes::BatReplacementNeeded::Id, BOOLEAN, 1, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(PowerSource::Attributes::BatReplaceability::Id, ENUM8, 1, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(PowerSource::Attributes::BatPresent::Id, BOOLEAN, 1, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(PowerSource::Attributes::EndpointList::Id, ARRAY, 0, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(PowerSource::Attributes::FeatureMap::Id, BITMAP32, 4, 0),
	DECLARE_DYNAMIC_ATTRIBUTE_LIST_END();

/* Descriptor is mandatory on every endpoint; the SDK answers for it itself, so
 * declaring the cluster is enough. Same as lock_cluster.cpp. */
DECLARE_DYNAMIC_ATTRIBUTE_LIST_BEGIN(sDescriptorAttrs)
DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::DeviceTypeList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::ServerList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::ClientList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::PartsList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE_LIST_END();

DECLARE_DYNAMIC_CLUSTER_LIST_BEGIN(sClusters)
DECLARE_DYNAMIC_CLUSTER(PowerSource::Id, sPowerAttrs, ZAP_CLUSTER_MASK(SERVER), nullptr, nullptr),
	DECLARE_DYNAMIC_CLUSTER(Descriptor::Id, sDescriptorAttrs, ZAP_CLUSTER_MASK(SERVER), nullptr,
				nullptr) DECLARE_DYNAMIC_CLUSTER_LIST_END;

DECLARE_DYNAMIC_ENDPOINT(sEndpoint, sClusters);
DataVersion sDataVersions[ArraySize(sClusters)];
const EmberAfDeviceType sDeviceTypes[] = { { kPowerSourceDeviceType, 1 } };

class BatteryAttrAccess : public AttributeAccessInterface {
public:
	BatteryAttrAccess() : AttributeAccessInterface(MakeOptional(kBatteryEndpoint), PowerSource::Id)
	{
	}

	CHIP_ERROR Read(const ConcreteReadAttributePath &path, AttributeValueEncoder &encoder) override
	{
		switch (path.mAttributeId) {
		case PowerSource::Attributes::Status::Id:
			return encoder.Encode(sStatus);
		case PowerSource::Attributes::Order::Id:
			/* The only source, so it is the first one. */
			return encoder.Encode(static_cast<uint8_t>(0));
		case PowerSource::Attributes::Description::Id:
			return encoder.Encode(CharSpan::fromCharString("CR2032"));
		case PowerSource::Attributes::BatVoltage::Id:
			/* Null until the first reading: a device that has been up
			 * for ten seconds has no measurement, and reporting 0 mV
			 * there reads as a dead cell. */
			if (sLastMv < 0) {
				return encoder.EncodeNull();
			}
			return encoder.Encode(static_cast<uint32_t>(sLastMv));
		case PowerSource::Attributes::BatPercentRemaining::Id:
			if (sLastMv < 0) {
				return encoder.EncodeNull();
			}
			return encoder.Encode(sHalfPercent);
		case PowerSource::Attributes::BatChargeLevel::Id:
			return encoder.Encode(sLevel);
		case PowerSource::Attributes::BatReplacementNeeded::Id:
			return encoder.Encode(sLevel == PowerSource::BatChargeLevelEnum::kCritical);
		case PowerSource::Attributes::BatReplaceability::Id:
			return encoder.Encode(PowerSource::BatReplaceabilityEnum::kUserReplaceable);
		case PowerSource::Attributes::BatPresent::Id:
			return encoder.Encode(true);
		case PowerSource::Attributes::EndpointList::Id:
			return encoder.Encode(DataModel::List<const EndpointId>(kPowered));
		case PowerSource::Attributes::FeatureMap::Id:
			return encoder.Encode(kFeatureBattery);
		case PowerSource::Attributes::ClusterRevision::Id:
			return encoder.Encode(kClusterRevision);
		default:
			return CHIP_IM_GLOBAL_STATUS(UnsupportedAttribute);
		}
	}
};

BatteryAttrAccess sAttrAccess;

int32_t ReadMillivolts()
{
	/* Save and restore rather than disable afterwards: POFCON belongs to the
	 * system, not to us. The flash driver turns POFWARN off around every
	 * write and expects to find its own setting when it looks again. */
	nrf_regulators_pof_config_t saved;
	nrf_regulators_pof_config_get(NRF_REGULATORS, &saved);

	nrf_regulators_pof_config_t cfg = saved;
	cfg.enable = true;
#if NRF_REGULATORS_HAS_POF_WARN_DISABLE
	/* Every threshold above the supply would otherwise raise POFWARN, and a
	 * sweep that starts at 2.8 V crosses several of them on the way down.
	 * The event says nothing here that the comparator has not already said
	 * directly. */
	cfg.warn_disable = true;
#endif

	int32_t found = -1;
	for (const Step &step : kSteps) {
		cfg.thr = step.thr;
		nrf_regulators_pof_config_set(NRF_REGULATORS, &cfg);
		k_busy_wait(kSettleUs);
		if (!nrf_regulators_pof_below_thr_check(NRF_REGULATORS)) {
			found = step.mv;
			break;
		}
	}

	nrf_regulators_pof_config_set(NRF_REGULATORS, &saved);
	return found;
}

void Report(AttributeId attr)
{
	MatterReportingAttributeChangeCallback(kBatteryEndpoint, PowerSource::Id, attr);
}

void Publish(int32_t mv)
{
	if (mv < 0) {
		/* Under the lowest step the supply is genuinely nearly gone, but
		 * the comparator can no longer say by how much - so the honest
		 * answer is "unavailable" rather than a number invented for the
		 * gauge. BatChargeLevel keeps whatever it last said, which was
		 * already Critical by the time it got here. */
		sStatus = PowerSource::PowerSourceStatusEnum::kUnavailable;
		Report(PowerSource::Attributes::Status::Id);
		return;
	}

	if (mv <= kEmptyMv) {
		sHalfPercent = 0;
	} else if (mv >= kFullMv) {
		sHalfPercent = kMaxHalfPercent;
	} else {
		sHalfPercent = static_cast<uint8_t>(kMaxHalfPercent * (mv - kEmptyMv) /
						    (kFullMv - kEmptyMv));
	}

	if (mv <= kCriticalMv) {
		sLevel = PowerSource::BatChargeLevelEnum::kCritical;
	} else if (mv <= kWarningMv) {
		sLevel = PowerSource::BatChargeLevelEnum::kWarning;
	} else {
		sLevel = PowerSource::BatChargeLevelEnum::kOk;
	}

	sLastMv = mv;
	sStatus = PowerSource::PowerSourceStatusEnum::kActive;

	/* Report everything that moves. Subscriptions are how the panel finds
	 * out - nothing polls a sleepy device once an hour. */
	Report(PowerSource::Attributes::Status::Id);
	Report(PowerSource::Attributes::BatVoltage::Id);
	Report(PowerSource::Attributes::BatPercentRemaining::Id);
	Report(PowerSource::Attributes::BatChargeLevel::Id);
	Report(PowerSource::Attributes::BatReplacementNeeded::Id);
}

void Measure()
{
	const int32_t mv = ReadMillivolts();
	if (mv < 0) {
		LOG_WRN("battery: below 1.7 V - the comparator's lowest step");
	} else {
		LOG_INF("battery: at least %d mV", mv);
	}

	/* Attribute writes belong to the Matter thread; this runs from a timer. */
	DeviceLayer::SystemLayer().ScheduleLambda([mv] { Publish(mv); });
}

/* A k_timer handler runs in the timer INTERRUPT, and neither half of a
 * measurement belongs there: the sweep busy-waits for up to 1.2 ms, and
 * scheduling onto the Matter thread allocates. So the timer does the one thing
 * it may - hand the job to a workqueue - and the work happens in thread
 * context. */
void WorkHandler(struct k_work *)
{
	Measure();
}

void TimerHandler(k_timer *)
{
	k_work_submit(&sWork);
}

} /* namespace */

namespace Battery {

void Init(void)
{
	/* The endpoint goes up before anything is measured. The composition is
	 * read once, at commissioning, so a device commissioned without the
	 * endpoint would never gain it - and until the first reading lands, the
	 * cluster answers null rather than a number. */
	CHIP_ERROR err = emberAfSetDynamicEndpoint(kDynamicIndex, kBatteryEndpoint, &sEndpoint,
						   Span<DataVersion>(sDataVersions),
						   Span<const EmberAfDeviceType>(sDeviceTypes));
	if (err != CHIP_NO_ERROR) {
		LOG_ERR("battery: cannot add the endpoint: %s", ErrorStr(err));
		return;
	}
	AttributeAccessInterfaceRegistry::Instance().Register(&sAttrAccess);

	/* NOTHING IS READ OR PUBLISHED HERE.
	 *
	 * This runs from the post-server-init callback, before the event loop is
	 * turning, and an early version measured and pushed attributes straight
	 * from it. A reading is worth nothing in the first half minute of a
	 * device that has been running for weeks, so it waits for the timer like
	 * every reading after it. Init only arranges for that to happen.
	 */
	k_work_init(&sWork, WorkHandler);
	k_timer_init(&sTimer, TimerHandler, nullptr);
	k_timer_start(&sTimer, K_SECONDS(30), K_MSEC(kIntervalMs));
}

int32_t LastMillivolts(void)
{
	return sLastMv;
}

} /* namespace Battery */
