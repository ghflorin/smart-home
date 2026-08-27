/*
 * battery - how much is left in the coin cell, published over Matter.
 *
 * WHY IT IS READ THIS WAY. The nRF54L's ADC can measure its own analogue
 * supply (NRF_SAADC_AVDD), so the cell is read through the chip rather than
 * through a resistor divider soldered across it. That is what makes this
 * retrofittable: the switches are already built, and it arrives as a firmware
 * update rather than a soldering job. A divider would also draw current for
 * ever, and on a cell rated in milliamp-hours that is the one cost you can
 * never earn back.
 *
 * WHY HOURLY. A CR2032 does not move in a minute, and the ADC is not free: the
 * reference and the sampler both draw current while enabled. The switch is
 * budgeted at around 10 uA average, where even a 180 ms LED pulse every ten
 * seconds costs more than the radio - so a reading every hour is generous, and
 * anything faster would be measuring the battery it was spending.
 *
 * WHY THE PERCENTAGE IS COARSE. A lithium coin cell holds close to 3 V for most
 * of its life and then falls off a cliff, so a percentage derived from voltage
 * is a rough guide and no more. The honest number is the voltage, which is
 * reported as-is; the percentage exists because that is what interfaces draw,
 * and BatChargeLevel is what to trust when it says Warning.
 */
#include "battery.h"

#include <app-common/zap-generated/attributes/Accessors.h>
#include <app-common/zap-generated/ids/Clusters.h>
#include <platform/CHIPDeviceLayer.h>

#include <zephyr/devicetree.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_DECLARE(app, CONFIG_SMARTHOME_LOG_LEVEL);

using namespace chip;
using namespace chip::app::Clusters;

namespace {

/* PowerSource lives on the root endpoint: it describes the DEVICE, not the
 * switch function on endpoint 1. */
constexpr EndpointId kPowerSourceEndpoint = 0;

/* A fresh CR2032 measures about 3.0 V at rest and a little more when brand new;
 * below roughly 2.2 V a Thread radio can no longer transmit reliably, so that
 * is treated as empty rather than the cell's own paper end-of-life. The two
 * thresholds in between are where the interface should start saying something.
 */
constexpr int32_t kFullMv = 3000;
constexpr int32_t kEmptyMv = 2200;
constexpr int32_t kWarningMv = 2500;
constexpr int32_t kCriticalMv = 2300;

/* Matter reports this in HALF percent: 0..200. */
constexpr uint8_t kMaxHalfPercent = 200;

constexpr int64_t kIntervalMs = 60 * 60 * 1000;

const struct adc_dt_spec kAdc = ADC_DT_SPEC_GET_BY_IDX(DT_PATH(zephyr_user), 0);

k_timer sTimer;
struct k_work sWork;
int32_t sLastMv = -1;

int32_t ReadMillivolts()
{
	int16_t sample = 0;
	struct adc_sequence seq = {
		.buffer = &sample,
		.buffer_size = sizeof(sample),
	};

	if (adc_sequence_init_dt(&kAdc, &seq) != 0) {
		return -1;
	}
	if (adc_read(kAdc.dev, &seq) != 0) {
		return -1;
	}

	int32_t mv = sample;
	if (adc_raw_to_millivolts_dt(&kAdc, &mv) != 0) {
		return -1;
	}
	return mv;
}

void Publish(int32_t mv)
{
	using namespace chip::app::Clusters::PowerSource;

	if (mv < 0) {
		/* Say "unavailable" rather than "0 V". A failed read is not a
		 * flat battery, and a gauge that draws empty because the ADC
		 * hiccupped sends somebody to fetch a cell that is not needed. */
		Attributes::Status::Set(kPowerSourceEndpoint, PowerSourceStatusEnum::kUnavailable);
		return;
	}

	uint8_t halfPercent;
	if (mv <= kEmptyMv) {
		halfPercent = 0;
	} else if (mv >= kFullMv) {
		halfPercent = kMaxHalfPercent;
	} else {
		halfPercent = static_cast<uint8_t>(kMaxHalfPercent * (mv - kEmptyMv) /
						   (kFullMv - kEmptyMv));
	}

	BatChargeLevelEnum level;
	if (mv < kCriticalMv) {
		level = BatChargeLevelEnum::kCritical;
	} else if (mv < kWarningMv) {
		level = BatChargeLevelEnum::kWarning;
	} else {
		level = BatChargeLevelEnum::kOk;
	}

	Attributes::Status::Set(kPowerSourceEndpoint, PowerSourceStatusEnum::kActive);
	Attributes::BatPresent::Set(kPowerSourceEndpoint, true);
	Attributes::BatVoltage::Set(kPowerSourceEndpoint, static_cast<uint32_t>(mv));
	Attributes::BatPercentRemaining::Set(kPowerSourceEndpoint, halfPercent);
	Attributes::BatChargeLevel::Set(kPowerSourceEndpoint, level);
	Attributes::BatReplacementNeeded::Set(kPowerSourceEndpoint, level == BatChargeLevelEnum::kCritical);
}

void Measure()
{
	sLastMv = ReadMillivolts();
	if (sLastMv < 0) {
		LOG_WRN("battery: read failed");
	} else {
		LOG_INF("battery: %d mV", sLastMv);
	}

	/* Attribute writes belong to the Matter thread; this runs from a timer. */
	const int32_t mv = sLastMv;
	DeviceLayer::SystemLayer().ScheduleLambda([mv] { Publish(mv); });
}

/* A k_timer handler runs in the timer INTERRUPT, where neither of the two
 * things a measurement does is allowed: adc_read() may block on the driver's
 * lock, and scheduling onto the Matter thread allocates. So the timer does the
 * one thing it may - hand the job to a workqueue - and the reading happens in
 * thread context. */
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
	if (!adc_is_ready_dt(&kAdc)) {
		LOG_WRN("battery: no ADC, readings disabled");
		return;
	}
	if (adc_channel_setup_dt(&kAdc) != 0) {
		LOG_ERR("battery: ADC channel setup failed");
		return;
	}

	/* NOTHING IS READ OR PUBLISHED HERE.
	 *
	 * This runs from the post-server-init callback, before the event loop is
	 * turning, and the first version measured and pushed attributes straight
	 * from it. The image that did so was transferred and applied correctly and
	 * then failed to come up: MCUboot reverted to the previous one, which is
	 * the bootloader doing its job and the reason the switch kept working.
	 *
	 * A battery reading is worth nothing in the first half minute of a device
	 * that has been running for weeks, so it waits for the timer like every
	 * reading after it. Init only arranges for that to happen.
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
