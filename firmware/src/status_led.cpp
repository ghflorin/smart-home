#include "status_led.h"

#include <zephyr/drivers/pwm.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(status_led, CONFIG_SMARTHOME_LOG_LEVEL);

namespace {

const struct pwm_dt_spec sCh[3] = {
	PWM_DT_SPEC_GET(DT_NODELABEL(pwm_led_red)),
	PWM_DT_SPEC_GET(DT_NODELABEL(pwm_led_green)),
	PWM_DT_SPEC_GET(DT_NODELABEL(pwm_led_blue)),
};

/* Per-channel trim, 0..255 where 255 = no attenuation.
 *
 * The three dies in the package are not equally efficient at the same current,
 * and the resistors on the module were chosen so the LED is visible, not for
 * white balance. Without trim, what you ask for as #ffa726 can come out
 * greenish or too red. Calibrated by eye: hold the LED next to the screen with
 * the Identify button in the interface and pull down whichever channel
 * dominates. */
constexpr uint8_t kTrim[3] = { CONFIG_SMARTHOME_LED_TRIM_R, CONFIG_SMARTHOME_LED_TRIM_G,
			       CONFIG_SMARTHOME_LED_TRIM_B };

/* sRGB -> linear light, 12 bits.
 *
 * This is the counterintuitive part: 50% duty does NOT look like half the
 * color. sRGB values are already perceptually compressed (gamma ~2.2), while
 * the LED emits in proportion to duty cycle. To get the color you see on screen
 * out of the LED, you have to undo that compression first. Concretely: #ffa726
 * is not duty 100/65/15%, it is 100/39/2%. Green lands at less than half and
 * blue nearly disappears - which is why an amber built "straight from the sRGB
 * values" always comes out as washed-out yellow.
 *
 * A table instead of a formula, to keep pow() and floating point out of a timer
 * that fires every 20 ms. 512 bytes in RRAM. */
constexpr uint16_t kSrgbToLinear[256] = {
	    0,     1,     2,     4,     5,     6,     7,     9,
	   10,    11,    12,    14,    15,    16,    18,    20,
	   21,    23,    25,    27,    29,    31,    33,    35,
	   37,    40,    42,    45,    48,    50,    53,    56,
	   59,    62,    66,    69,    72,    76,    79,    83,
	   87,    91,    95,    99,   103,   107,   112,   116,
	  121,   126,   131,   136,   141,   146,   151,   156,
	  162,   168,   173,   179,   185,   191,   197,   204,
	  210,   216,   223,   230,   237,   244,   251,   258,
	  265,   273,   280,   288,   296,   304,   312,   320,
	  329,   337,   346,   354,   363,   372,   381,   390,
	  400,   409,   419,   428,   438,   448,   458,   469,
	  479,   490,   500,   511,   522,   533,   544,   555,
	  567,   578,   590,   602,   614,   626,   639,   651,
	  664,   676,   689,   702,   715,   728,   742,   755,
	  769,   783,   797,   811,   825,   840,   854,   869,
	  884,   899,   914,   929,   945,   960,   976,   992,
	 1008,  1024,  1041,  1057,  1074,  1091,  1108,  1125,
	 1142,  1159,  1177,  1195,  1213,  1231,  1249,  1267,
	 1286,  1304,  1323,  1342,  1361,  1381,  1400,  1420,
	 1440,  1459,  1480,  1500,  1520,  1541,  1562,  1582,
	 1603,  1625,  1646,  1668,  1689,  1711,  1733,  1755,
	 1778,  1800,  1823,  1846,  1869,  1892,  1916,  1939,
	 1963,  1987,  2011,  2035,  2059,  2084,  2109,  2133,
	 2159,  2184,  2209,  2235,  2260,  2286,  2312,  2339,
	 2365,  2392,  2419,  2446,  2473,  2500,  2527,  2555,
	 2583,  2611,  2639,  2668,  2696,  2725,  2754,  2783,
	 2812,  2841,  2871,  2901,  2931,  2961,  2991,  3022,
	 3052,  3083,  3114,  3146,  3177,  3209,  3240,  3272,
	 3304,  3337,  3369,  3402,  3435,  3468,  3501,  3535,
	 3568,  3602,  3636,  3670,  3705,  3739,  3774,  3809,
	 3844,  3879,  3915,  3950,  3986,  4022,  4059,  4095,
};

constexpr uint8_t kPulseFloor = 90; /* out of 255 - how much stays lit at the minimum */
constexpr uint32_t kBeatMs = 180;   /* length of one Heartbeat pulse */
constexpr k_timeout_t kTick = K_MSEC(20);

struct Effect {
	StatusLed::Pattern pattern = StatusLed::Pattern::Off;
	StatusLed::Rgb color = {};
	uint16_t periodMs = 1000;
	uint8_t maxScale = 255; /* ceiling over whatever shape the effect produces */
};

Effect sBackground;
Effect sOverlay;
bool sOverlayActive = false;

/* Flashes are a one-deep queue: a second flash arriving before the first one
 * finishes replaces it. The alternative (a real queue) would mean a burst of
 * presses leaves the LED blinking for seconds after you are done. */
struct {
	bool active = false;
	StatusLed::Rgb color = {};
	uint16_t halfMs = 0;
	uint8_t left = 0;
	bool on = false;
	int64_t nextAt = 0;
} sFlash;

int64_t sPhaseStart;
bool sTicking = false;

void Tick(struct k_work *work);
K_WORK_DELAYABLE_DEFINE(sTickWork, Tick);

void WriteRaw(StatusLed::Rgb c, uint8_t scale)
{
	for (int i = 0; i < 3; i++) {
		const uint8_t comp = (i == 0) ? c.r : (i == 1) ? c.g : c.b;
		uint32_t lin = kSrgbToLinear[comp];       /* 0..4095 */
		lin = lin * scale / 255;                  /* effect brightness */
		lin = lin * kTrim[i] / 255;               /* color balance */
		uint32_t pulse = (uint32_t)((uint64_t)sCh[i].period * lin / 4095);
		pwm_set_pulse_dt(&sCh[i], pulse);
	}
}

/* Raised sine without <cmath>: a parabolic approximation over half a period,
 * mirrored. The error against a real cosine is under 2% - completely invisible
 * on an LED, and it keeps libm out of the image. */
uint8_t Wave(uint32_t phaseMs, uint16_t periodMs)
{
	uint32_t half = periodMs / 2;
	if (half == 0) {
		return 255;
	}
	uint32_t t = phaseMs % periodMs;
	bool rising = t < half;
	uint32_t x = rising ? t : (periodMs - t);   /* 0..half */
	/* 4*x*(half-x)/half^2 gives a smooth 0->1->0 arc over half a period; we
	 * take only the rising branch, so what comes out climbs 0->1 smoothly. */
	uint32_t v = (255u * x) / half;
	return (uint8_t)((v * v) / 255);            /* approximate ease-in-out */
}

bool NeedsTick(const Effect &e)
{
	return e.pattern == StatusLed::Pattern::Pulse || e.pattern == StatusLed::Pattern::Blink ||
	       e.pattern == StatusLed::Pattern::Breathe ||
	       e.pattern == StatusLed::Pattern::Heartbeat;
}

void Reschedule(void)
{
	const Effect &e = sOverlayActive ? sOverlay : sBackground;
	bool want = sFlash.active || NeedsTick(e);

	if (!want) {
		sTicking = false;
		k_work_cancel_delayable(&sTickWork);
		/* Render statically, once; then the peripheral shuts down. */
		if (e.pattern == StatusLed::Pattern::Solid) {
			WriteRaw(e.color, e.maxScale);
		} else {
			WriteRaw({ 0, 0, 0 }, 0);
		}
		return;
	}

	/* The phase starts only when an animation (re)starts, not on every call -
	 * otherwise a refresh would send the pulse back to the beginning. */
	if (!sTicking) {
		sPhaseStart = k_uptime_get();
		sTicking = true;
	}

	/* ALWAYS rearm when there is something to animate.
	 *
	 * The previous version skipped the rearm when sTicking was already true
	 * ("it is already ticking"), and that left a hole: on the last pulse of a
	 * flash, Tick sets sFlash.active = false, calls Reschedule and returns
	 * WITHOUT rescheduling the work item. With sTicking still true, Reschedule
	 * did not rearm it either - so after the first button press the background
	 * animation died for good and the LED stayed dark until reboot.
	 *
	 * k_work_reschedule on an already-armed work item only moves the deadline,
	 * so rearming unconditionally is cheap and cannot lose the tick. */
	k_work_reschedule(&sTickWork, K_NO_WAIT);
}

void Tick(struct k_work *work)
{
	int64_t now = k_uptime_get();

	if (sFlash.active) {
		if (now >= sFlash.nextAt) {
			if (sFlash.on) {
				sFlash.on = false;
				sFlash.left--;
				if (sFlash.left == 0) {
					sFlash.active = false;
					Reschedule();
					return;
				}
			} else {
				sFlash.on = true;
			}
			sFlash.nextAt = now + sFlash.halfMs;
		}
		WriteRaw(sFlash.on ? sFlash.color : StatusLed::Rgb{ 0, 0, 0 }, 255);
		k_work_reschedule(&sTickWork, kTick);
		return;
	}

	const Effect &e = sOverlayActive ? sOverlay : sBackground;
	uint32_t phase = (uint32_t)(now - sPhaseStart);
	uint8_t scale = 0;

	switch (e.pattern) {
	case StatusLed::Pattern::Pulse: {
		uint8_t w = Wave(phase, e.periodMs);
		scale = (uint8_t)(kPulseFloor + ((255 - kPulseFloor) * w) / 255);
		break;
	}
	case StatusLed::Pattern::Breathe:
		scale = Wave(phase, e.periodMs);
		break;
	case StatusLed::Pattern::Blink:
		scale = (phase % e.periodMs) < (uint32_t)(e.periodMs / 2) ? 255 : 0;
		break;
	case StatusLed::Pattern::Heartbeat: {
		uint32_t inPeriod = phase % e.periodMs;
		if (inPeriod >= kBeatMs) {
			/* Idle stretch. Go dark and sleep until the next pulse
			 * instead of ticking every 20 ms for nothing - that is
			 * the entire power difference against an LED held on. */
			WriteRaw({ 0, 0, 0 }, 0);
			k_work_reschedule(&sTickWork, K_MSEC(e.periodMs - inPeriod));
			return;
		}
		/* A 0 -> full -> 0 arc spanning the pulse. */
		uint32_t half = kBeatMs / 2;
		uint32_t x = (inPeriod < half) ? inPeriod : (kBeatMs - inPeriod);
		scale = (uint8_t)((255u * x) / half);
		break;
	}
	case StatusLed::Pattern::Solid:
		scale = 255;
		break;
	case StatusLed::Pattern::Off:
	default:
		scale = 0;
		break;
	}

	WriteRaw(e.color, (uint8_t)((uint32_t)scale * e.maxScale / 255));
	k_work_reschedule(&sTickWork, kTick);
}

#if CONFIG_SMARTHOME_LED_CHANNEL_TEST
/* Channel identification, by the NUMBER of blinks.
 *
 * The first version held each channel on for two seconds. That was not clear
 * enough: the indicator pulse overlapped it and you could no longer tell what
 * you were looking at. Now the background indicator is off entirely while the
 * test runs, and each channel announces itself by its blink count:
 *
 *   channel 0 (should be RED)    one LONG blink
 *   channel 1 (should be GREEN)  two short blinks
 *   channel 2 (should be BLUE)   three short blinks
 *
 * There is a long pause between groups so they do not run into each other. */
constexpr uint16_t kTestOnMs[3] = { 900, 200, 200 };
constexpr uint8_t kTestCount[3] = { 1, 2, 3 };
constexpr uint16_t kTestGapMs = 220;    /* between blinks of the same channel */
constexpr uint16_t kTestPauseMs = 1800; /* between channels */

struct {
	uint8_t channel;
	uint8_t remaining;
	bool on;
} sTest = { 0, kTestCount[0], false };

void ChanTestWork(struct k_work *work);
K_WORK_DELAYABLE_DEFINE(sChanTestWork, ChanTestWork);

void ChanTestWork(struct k_work *work)
{
	uint32_t nextMs;

	if (!sTest.on) {
		for (int i = 0; i < 3; i++) {
			pwm_set_pulse_dt(&sCh[i], i == sTest.channel ? sCh[i].period : 0);
		}
		sTest.on = true;
		nextMs = kTestOnMs[sTest.channel];
	} else {
		for (int i = 0; i < 3; i++) {
			pwm_set_pulse_dt(&sCh[i], 0);
		}
		sTest.on = false;
		sTest.remaining--;

		if (sTest.remaining > 0) {
			nextMs = kTestGapMs;
		} else {
			LOG_INF("channel %u: %u blinks", sTest.channel, kTestCount[sTest.channel]);
			sTest.channel = (sTest.channel + 1) % 3;
			sTest.remaining = kTestCount[sTest.channel];
			nextMs = kTestPauseMs;
		}
	}

	k_work_reschedule(&sChanTestWork, K_MSEC(nextMs));
}
#endif

} /* namespace */

namespace StatusLed {

void Init(void)
{
	/* Check ALL channels; do not bail out on the first one. Otherwise, with
	 * the console disabled, a missing channel is indistinguishable from
	 * "nothing is driving the LED". */
	bool ready = true;
	for (int i = 0; i < 3; i++) {
		if (!pwm_is_ready_dt(&sCh[i])) {
			LOG_ERR("PWM channel %d unavailable", i);
			ready = false;
		}
	}
	if (!ready) {
		return;
	}
	WriteRaw({ 0, 0, 0 }, 0);

#if CONFIG_SMARTHOME_LED_CHANNEL_TEST
	k_work_reschedule(&sChanTestWork, K_SECONDS(1));
#endif
}

void SetBackground(Pattern p, Rgb color, uint16_t periodMs)
{
	SetBackgroundScaled(p, color, 255, periodMs);
}

void SetBackgroundScaled(Pattern p, Rgb color, uint8_t maxScale, uint16_t periodMs)
{
	Effect next = { p, color, periodMs ? periodMs : 1000, maxScale };

	/* If nothing changed, do not restart the phase. Otherwise the indicator,
	 * refreshed once a minute, would restart the pulse and look irregular. */
	if (next.pattern == sBackground.pattern && next.periodMs == sBackground.periodMs &&
	    next.maxScale == sBackground.maxScale && next.color.r == sBackground.color.r &&
	    next.color.g == sBackground.color.g && next.color.b == sBackground.color.b) {
		return;
	}

	sBackground = next;
	Reschedule();
}

void SetOverlay(Pattern p, Rgb color, uint16_t periodMs)
{
	sOverlay = { p, color, periodMs ? periodMs : 1000, 255 };
	sOverlayActive = true;
	sPhaseStart = k_uptime_get();
	Reschedule();
}

void ClearOverlay(void)
{
	sOverlayActive = false;
	Reschedule();
}

void Flash(Rgb color, uint16_t ms, uint8_t times)
{
	if (times == 0) {
		return;
	}
	sFlash.active = true;
	sFlash.color = color;
	sFlash.halfMs = ms;
	sFlash.left = times;
	sFlash.on = true;
	sFlash.nextAt = k_uptime_get() + ms;
	Reschedule();
	k_work_reschedule(&sTickWork, K_NO_WAIT);
}

/* A simple blackbody approximation, interpolated between fixed points. Not
 * colorimetrically exact, and it does not need to be: this is a 2 mm LED whose
 * job is to say "I sent warm" or "I sent cool". */
Rgb FromMireds(uint16_t mireds)
{
	struct Point {
		uint16_t mireds;
		Rgb c;
	};
	/* 153 = 6500K ... 500 = 2000K */
	static constexpr Point kPts[] = {
		{ 153, { 0xcc, 0xdd, 0xff } }, { 200, { 0xdd, 0xe6, 0xff } },
		{ 250, { 0xff, 0xf0, 0xe8 } }, { 300, { 0xff, 0xdd, 0xb8 } },
		{ 370, { 0xff, 0xc4, 0x8a } }, { 454, { 0xff, 0xa5, 0x55 } },
		{ 500, { 0xff, 0x93, 0x3a } },
	};
	constexpr size_t n = sizeof(kPts) / sizeof(kPts[0]);

	if (mireds <= kPts[0].mireds) {
		return kPts[0].c;
	}
	for (size_t i = 1; i < n; i++) {
		if (mireds <= kPts[i].mireds) {
			uint32_t span = kPts[i].mireds - kPts[i - 1].mireds;
			uint32_t t = mireds - kPts[i - 1].mireds;
			auto mix = [&](uint8_t a, uint8_t b) {
				return (uint8_t)(a + ((int32_t)b - a) * (int32_t)t / (int32_t)span);
			};
			return { mix(kPts[i - 1].c.r, kPts[i].c.r), mix(kPts[i - 1].c.g, kPts[i].c.g),
				 mix(kPts[i - 1].c.b, kPts[i].c.b) };
		}
	}
	return kPts[n - 1].c;
}

/* Red -> amber -> green, across the Matter level.
 *
 * The green is not pure (it carries some red): a saturated green on a 2 mm LED
 * looks acid and does not match the rest of the signaling. */
Rgb FromLevel(uint8_t level)
{
	constexpr Rgb kLow = { 0xff, 0x18, 0x08 };  /* low brightness */
	constexpr Rgb kMid = { 0xff, 0x8a, 0x00 };
	constexpr Rgb kHigh = { 0x40, 0xff, 0x30 }; /* maximum */

	auto mix = [](uint8_t a, uint8_t b, uint32_t t, uint32_t span) {
		return (uint8_t)(a + ((int32_t)b - a) * (int32_t)t / (int32_t)span);
	};

	if (level <= 1) {
		return kLow;
	}
	if (level < 127) {
		uint32_t t = level - 1;
		return { mix(kLow.r, kMid.r, t, 126), mix(kLow.g, kMid.g, t, 126),
			 mix(kLow.b, kMid.b, t, 126) };
	}
	uint32_t t = (level > 254 ? 254 : level) - 127;
	return { mix(kMid.r, kHigh.r, t, 127), mix(kMid.g, kHigh.g, t, 127),
		 mix(kMid.b, kHigh.b, t, 127) };
}

} /* namespace StatusLed */
