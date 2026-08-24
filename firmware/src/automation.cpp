/*
 * automation - everything that happens WITHOUT pressing the button, plus the
 * accelerometer gestures.
 *
 * Three classes of trigger:
 *   1. periodic - reasserting the startup state in the bulbs
 *   2. temporal - scenes at fixed times (simulated sunrise/sunset, night mode)
 *   3. sensor   - tap / shake / orientation on the accelerometer
 */
#include "automation.h"
#include "light_ctrl.h"
#include "lock_state.h"
#include "status_led.h"

#include <app/server/Server.h>

#include <zephyr/device.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(automation, CONFIG_SMARTHOME_LOG_LEVEL);

namespace {

/* ---- 1. Reasserting the startup state ----------------------------------
 * The bulb keeps StartUpOnOff / StartUpCurrentLevel internally, so writing it
 * once would normally be enough. We rewrite it periodically as a defense
 * against:
 *   - an accidental factory reset of the bulb
 *   - an IKEA OTA firmware update that resets the attributes
 *   - another admin in the fabric changing them
 *
 * We do NOT write it on every button press: that is pointless traffic and a
 * write to the bulb's flash, which has a finite number of cycles. */
void StartupStateWork(struct k_work *work);
K_WORK_DELAYABLE_DEFINE(sStartupStateWork, StartupStateWork);

void StartupStateWork(struct k_work *work)
{
	/* 0xFF for onOff = null = the bulb returns to exactly the state it had
	 * before the outage. For "always on after an outage", use 1.
	 * 0xFF for level = return to the brightness from before the outage. */
	LightCtrl::WriteStartupState(CONFIG_SMARTHOME_STARTUP_ONOFF,
				     CONFIG_SMARTHOME_STARTUP_LEVEL);

	k_work_reschedule(&sStartupStateWork, K_HOURS(CONFIG_SMARTHOME_STARTUP_REFRESH_HOURS));
}

/* ---- 2. The permanent indicator ----------------------------------------
 * The LED shows what the switch would do if you pressed it NOW: the color comes
 * from the level scheduled for the current time (red at low brightness, green
 * at maximum), and the LED's brightness follows that same level. So late at
 * night it is a very faint red, and during the day a strong green.
 *
 * Locked = fully dark. No blink, no response to a press. That is also the only
 * hint that the switch is dead on purpose rather than broken.
 *
 * The level changes a few times a day, but slot boundaries can sit anywhere, so
 * we check once a minute. It is one comparison and nothing else - no radio, no
 * PWM, no peripheral woken up. */
Automation::NetState sNetState = Automation::NetState::Normal;

void IndicatorWork(struct k_work *work);
K_WORK_DELAYABLE_DEFINE(sIndicatorWork, IndicatorWork);

void IndicatorWork(struct k_work *work)
{
	Automation::RefreshIndicator();
	k_work_reschedule(&sIndicatorWork, K_MINUTES(1));
}

/* ---- 3. Accelerometer --------------------------------------------------
 * The Zephyr sensor API, so this does not care which chip the Holyiot module
 * actually carries (LIS2DH12 / LIS3DH / QMA6100P - only the overlay changes).
 *
 * DEVICE_DT_GET_ONE is a compile-time macro: if the board has no such node in
 * devicetree, the build fails. That is why the whole block sits under
 * DT_HAS_COMPAT_STATUS_OKAY, so it also builds on nrf54l15dk (which has no
 * accelerometer). */
#define ACCEL_COMPAT st_lis2dh
#define HAS_ACCEL DT_HAS_COMPAT_STATUS_OKAY(ACCEL_COMPAT)

/* The power-down result, kept so it can be checked over SWD. The switch has no
 * console, and "the sensor is off" is exactly the kind of claim you should not
 * take on faith when battery life depends on it.
 *
 * volatile because nothing in the firmware reads it, and without that LTO
 * eliminates it entirely - i.e. the instrument you use to verify disappears
 * precisely when you need it. */
volatile int sAccelPowerDownResult = -EAGAIN;

#if HAS_ACCEL && CONFIG_SMARTHOME_ACCEL_GESTURES
const struct device *sAccel;

/* The tap threshold is not set here: it is a driver property, configured in the
 * board overlay (e.g. anym-on-int1 / threshold on lis2dh). */
constexpr int64_t kDoubleTapWindowMs = 400;
int64_t sLastTapMs = 0;

void AccelTrigger(const struct device *dev, const struct sensor_trigger *trig)
{
	int64_t now = k_uptime_get();

	if (now - sLastTapMs < kDoubleTapWindowMs) {
		/* Double tap used to cycle through modes. The modes went away
		 * along with the schedule on the switch, and the accelerometer is
		 * powered down on purpose, so there is nothing left to do here.
		 * The handler is kept as a starting point should the sensor come
		 * back. */
		sLastTapMs = 0;
	} else {
		sLastTapMs = now;
	}
}

int AccelInit(void)
{
	sAccel = DEVICE_DT_GET_ONE(ACCEL_COMPAT);
	if (!device_is_ready(sAccel)) {
		LOG_WRN("accelerometer unavailable - gestures disabled");
		return -ENODEV;
	}

	struct sensor_trigger trig = {
		.type = SENSOR_TRIG_TAP,
		.chan = SENSOR_CHAN_ACCEL_XYZ,
	};
	int rc = sensor_trigger_set(sAccel, &trig, AccelTrigger);
	if (rc) {
		/* Not every driver exposes SENSOR_TRIG_TAP. Fall back to
		 * SENSOR_TRIG_DELTA (motion) when tap is unsupported. */
		LOG_WRN("tap trigger unsupported (%d), trying delta", rc);
		trig.type = SENSOR_TRIG_DELTA;
		rc = sensor_trigger_set(sAccel, &trig, AccelTrigger);
	}
	return rc;
}
#else  /* no gestures */
int AccelInit(void)
{
	return -ENOTSUP;
}
#endif

/* Powers the accelerometer down.
 *
 * The driver starts it during initialization (it writes the ODR), so simply not
 * using it is not enough - it has to be stopped explicitly. ODR = 0 means
 * power-down. */
void AccelPowerDown(void)
{
#if HAS_ACCEL
	const struct device *dev = DEVICE_DT_GET_ONE(ACCEL_COMPAT);

	if (!device_is_ready(dev)) {
		LOG_ERR("accelerometer unavailable - could NOT power it down");
		sAccelPowerDownResult = -ENODEV;
		return;
	}

	struct sensor_value odr = { .val1 = 0, .val2 = 0 };
	sAccelPowerDownResult = sensor_attr_set(dev, SENSOR_CHAN_ACCEL_XYZ,
						SENSOR_ATTR_SAMPLING_FREQUENCY, &odr);
	if (sAccelPowerDownResult) {
		LOG_ERR("cannot power down the accelerometer: %d", sAccelPowerDownResult);
	} else {
		LOG_INF("accelerometer powered down (ODR=0)");
	}
#else
	sAccelPowerDownResult = -ENOTSUP;
#endif
}

} /* namespace */

namespace Automation {

void Init(void)
{
	/* The network state is READ at startup; we do not wait for an event to
	 * tell us.
	 *
	 * The first version relied solely on the BLE advertising event. If that
	 * fired before the handler was registered - exactly what happened on the
	 * board - the switch was left with a dark LED indefinitely, which is exactly
	 * when you most need to see that it is alive.
	 *
	 * Here we are inside the post-server-init callback, on the Matter thread
	 * with the lock held, so we can query the fabric table directly. */
	sNetState = (chip::Server::GetInstance().GetFabricTable().FabricCount() == 0)
			    ? NetState::Commissioning
			    : NetState::Normal;
	LOG_INF("startup: %s",
		sNetState == NetState::Commissioning ? "not commissioned" : "in a fabric");

	/* The indicator is applied NOW, not three seconds later through the work
	 * queue.
	 *
	 * The delayed-work version was wrong for two reasons. A practical one: at
	 * startup you want to see immediately that the switch is alive, not after
	 * a pause during which it looks dead. A debugging one: if the work item
	 * fails to fire for any reason, the LED stays dark and you cannot tell
	 * that apart from a fault on the PWM path. The periodic work item stays,
	 * but only to catch slot boundaries in the table. */
	RefreshIndicator();

	/* The first startup state write, after CASE has probably been
	 * established. */
	k_work_reschedule(&sStartupStateWork, K_SECONDS(45));
	k_work_reschedule(&sIndicatorWork, K_MINUTES(1));

	/* Power the sensor down first, then possibly bring it back up for
	 * gestures. The order matters: with gestures disabled, the sensor stays
	 * powered down. */
	AccelPowerDown();
	AccelInit();
}

void SetNetState(NetState s)
{
	sNetState = s;
	RefreshIndicator();
}

/* ALL decisions about the background layer funnel through here, in this
 * priority order. The first version wrote the commissioning pattern straight
 * from the Matter event handler, and the indicator wiped it on the first tick:
 * the LED breathed blue for three seconds after boot, then went dark forever.
 * Two things writing the same layer with no ordering between them can only be
 * fixed by moving them into the same place. */
void RefreshIndicator(void)
{
#if CONFIG_SMARTHOME_LED_CHANNEL_TEST
	/* The channel test owns the LED outright. */
	return;
#else
	/* The LED now has a single background job: to say whether the switch is
	 * waiting to be added to a network. Otherwise it stays DARK.
	 *
	 * It used to pulse every few seconds and show, through brightness, which
	 * level it would send the bulb at the current time. That stopped making
	 * sense once the schedule moved to the Raspberry Pi: the switch no longer
	 * decides the level, so it has nothing to announce. And a pulse every few
	 * seconds on a battery device costs more than the radio.
	 *
	 * Press feedback is still there - a short flash - but that is an event,
	 * not a permanent indicator. */
	if (sNetState == NetState::Commissioning) {
		/* Blink, not breathe: Breathe follows a squared curve, so it is
		 * bright only around the peak and the rest of the cycle sits
		 * below the visible threshold. On the board it read as "it lit up
		 * for two seconds and went out" - exactly what you do not want
		 * from an indicator that means "waiting to be added". */
		StatusLed::SetBackground(StatusLed::Pattern::Blink, StatusLed::kBlue, 1200);
		return;
	}

	StatusLed::SetBackground(StatusLed::Pattern::Off, {});
#endif
}

void OnButtonShortPress(void)
{

	/* The lock role: this switch turns nothing on. It flips its own state and
	 * sends it to every node in its binding table. It sends ITS VALUE, not a
	 * per-node toggle - otherwise the switches would drift out of sync as soon
	 * as one of them missed a command. */
	if (LockState::GetRole() == LockState::Role::Lock) {
		bool next = !LockState::IsLocked();
		LockState::SetLocked(next, true);
		LightCtrl::WriteLock(next);
		/* The confirmation goes to the lock's own LED, the only one that
		 * stays alive. */
		StatusLed::Flash(next ? StatusLed::kRed : StatusLed::kTeal, 160, 2);
		return;
	}

	if (LockState::IsLocked()) {
		/* Completely dead, on purpose: not even a blink. A visible
		 * response would turn a locked switch into a toy. */
		return;
	}

	/* TOGGLE, not an explicit on/off.
	 *
	 * The switch no longer tracks whether the bulb is on - and it has no way
	 * to know, if you turned it on from the panel or from another ecosystem. A
	 * local `static bool` went stale the moment a command arrived from
	 * anywhere else, and after that a press looked like it did nothing.
	 *
	 * The level and color temperature for the current time are NOT sent from
	 * here any more. The Raspberry Pi puts them into the bulb's own persistent
	 * attributes (OnLevel and the color temperature) on every slot change. So
	 * the bulb already knows what to come up at, and the switch has exactly one
	 * job.
	 *
	 * Three wins, not one: the state can no longer drift out of sync; the bulb
	 * no longer ramps from the old brightness to the new one, so the flash is
	 * gone; and the switch sends one small command instead of three, which
	 * matters on battery.
	 *
	 * What is lost: the schedule no longer works with the Raspberry Pi off.
	 * The bulbs stay at the last value they received instead of following the
	 * time of day. A deliberate choice, explicitly requested. */
	LightCtrl::Toggle();

	/* One confirmation, deliberately neutral: we do not know which way the
	 * bulb switched, and pretending we do would be exactly the lie we just
	 * removed. */
	StatusLed::Flash(StatusLed::kTeal, 90);

	RefreshIndicator();
}

void OnButtonLongPress(void)
{
	if (LockState::GetRole() == LockState::Role::Lock || LockState::IsLocked()) {
		return;
	}

	/* Long press = maximum NOW, once. Not a persistent mode.
	 *
	 * It used to be a mode that pinned the level and color until you left it,
	 * and leaving it meant a double tap on the accelerometer - a sensor we
	 * power down on purpose, so the mode stayed stuck until you pulled the
	 * battery.
	 *
	 * No mode is needed at all now: to get back to the schedule for the
	 * current time, turn the bulb off and on. It comes up at the value the
	 * Raspberry Pi wrote for the current slot, because the bulb holds it, not
	 * the switch. */
	LightCtrl::SetLevel(254, 2);
	LightCtrl::SetColorTemp(250, 2);  /* 4000K, task lighting */
	StatusLed::Flash(StatusLed::kTeal, 130, 2);
}

} /* namespace Automation */
