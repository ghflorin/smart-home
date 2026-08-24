/*
 * status_led - the RGB LED on the module, used for signaling.
 *
 * The Holyiot module carries ONE RGB LED, i.e. three separate channels (red
 * P2.09, green P1.10, blue P2.07), all active-low. We drive all three with PWM
 * from the same instance (pwm20), so we get color and brightness rather than
 * just on/off.
 *
 * This is NOT an addressable WS2812-style LED - there is no controller chip,
 * just three dies in one package. So there is no "protocol", only three duty
 * cycles.
 *
 * Power: the pwm_nrfx driver shuts the peripheral down and parks the pin in
 * GPIO when duty is 0 or 100%, so an LED that is off costs nothing. An animated
 * effect keeps the peripheral running, so long effects are used only where they
 * earn it (Identify, commissioning). Routine feedback is a single short flash.
 */
#pragma once

#include <cstdint>

namespace StatusLed {

/* Color in sRGB, exactly as in the web interface - #ffa726 is written
 * {0xff, 0xa7, 0x26}. The conversion to duty cycle happens internally and is
 * NOT linear; see the kSrgbToLinear comment in the .cpp. */
struct Rgb {
	uint8_t r, g, b;
};

/* The same values as the CSS variables in panel/index.html. If you change them
 * there, change them here too - otherwise Identify on the switch is no longer
 * the same color as the Identify button in the interface. */
constexpr Rgb kAmber = { 0xff, 0xa7, 0x26 }; /* --amber, dark theme */
constexpr Rgb kTeal = { 0x45, 0xc4, 0xb0 };  /* --link */
constexpr Rgb kRed = { 0xe5, 0x53, 0x4b };   /* --bad */
/* PURE blue, no green at all.
 *
 * The first attempt was {0x4a, 0x8f, 0xff} - a "screen" blue with a little
 * green in it to make it read brighter. It looks right on a monitor; on the LED
 * it came out GREEN. The reason is physical: even with blue at 100% duty and
 * green at 27%, the blue die is several times less efficient than the green one
 * at the same current, so green dominates.
 *
 * But PURE blue was too weak to see: 100% duty on the weakest die is still not
 * much light. The current compromise keeps 0x40 of green, about 5% duty -
 * enough to borrow light from the efficient die, too little to drag the hue
 * toward green.
 *
 * The rule for cheap RGB LEDs: do not copy colors off a screen. What looks good
 * on a monitor does not look the same on three dies with different efficiencies,
 * and maximum saturation is not automatically the answer either - a weak die at
 * 100% is still weak. */
constexpr Rgb kBlue = { 0x00, 0x40, 0xff };

enum class Pattern : uint8_t {
	Off,
	Solid,
	/* A sine between kPulseFloor and full. This reproduces the "beacon"
	 * animation in the interface: the button stays amber and its glow
	 * pulses instead of going dark between pulses. */
	Pulse,
	Blink,   /* square wave, 50% duty */
	Breathe, /* like Pulse, but drops all the way to off */
	/* One short pulse every `periodMs`, then dark until the next one.
	 *
	 * This is the only shape in which a permanent indicator is affordable on
	 * battery. An LED held on under PWM costs ~1.5 mA in the peripheral
	 * alone, because PWM requires HFCLK to stay up - that is a battery life
	 * measured in days. With a 180 ms pulse every 10 s the peripheral is up
	 * 1.8% of the time. Between pulses we do not even tick: the work item
	 * reschedules itself straight to the next pulse. */
	Heartbeat,
};

void Init(void);

/* The background effect: the state the device is in (not commissioned,
 * commissioning window open...). Persists until you change it. */
void SetBackground(Pattern p, Rgb color, uint16_t periodMs = 1000);

/* Like SetBackground, but with a brightness ceiling (0..255) applied over the
 * effect. It serves the permanent indicator: the color says which level comes
 * next, and the ceiling makes the LED emit in proportion to the bulbs - a
 * glimmer at night, full brightness during the day. */
void SetBackgroundScaled(Pattern p, Rgb color, uint8_t maxScale, uint16_t periodMs);

/* Priority effect, which hides the background while it is active. For Identify. */
void SetOverlay(Pattern p, Rgb color, uint16_t periodMs = 1000);
void ClearOverlay(void);

/* One short pulse, then back to whatever was there before. For confirmations:
 * command sent, command failed, mode changed. */
void Flash(Rgb color, uint16_t ms = 120, uint8_t times = 1);

/* Approximates the color of a color temperature, so the LED can blink in the
 * shade you just sent to the bulbs. mireds = 1,000,000 / kelvin. */
Rgb FromMireds(uint16_t mireds);

/* The color that corresponds to a Matter level: red at low brightness, amber in
 * the middle, green at maximum. So you look at the switch and know what it would
 * do if you pressed it, without turning anything on. */
Rgb FromLevel(uint8_t level);

} /* namespace StatusLed */
