/*
 * Copyright (c) 2022 Nordic Semiconductor ASA
 *
 * SPDX-License-Identifier: LicenseRef-Nordic-5-Clause
 */

#include "app_task.h"

#include "automation.h"
#include "battery.h"
#include "light_ctrl.h"
#include "lock_state.h"
#include "lock_cluster.h"
#include "status_led.h"

#include "app/matter_init.h"
#include "app/task_executor.h"
#include "board/board.h"

#include <app/clusters/identify-server/identify-server.h>
#include <setup_payload/OnboardingCodesUtil.h>

#include <zephyr/logging/log.h>

LOG_MODULE_DECLARE(app, CONFIG_CHIP_APP_LOG_LEVEL);

using namespace ::chip;
using namespace ::chip::app;
using namespace ::chip::DeviceLayer;

namespace
{
constexpr uint32_t kDimmerTriggeredTimeout = 500;
constexpr uint32_t kDimmerInterval = 300;
constexpr EndpointId kLightSwitchEndpointId = 1;
constexpr EndpointId kLightEndpointId = 1;

k_timer sDimmerPressKeyTimer;
k_timer sDimmerTimer;

Identify sIdentify = { kLightEndpointId, AppTask::IdentifyStartHandler, AppTask::IdentifyStopHandler,
		       Clusters::Identify::IdentifyTypeEnum::kVisibleIndicator };
bool sWasDimmerTriggered = false;

/* Initialization results, kept so they can be read over SWD. The switch has no
 * console in the production build, so without this a startup failure is
 * completely invisible - all you see is "it does nothing". */
CHIP_ERROR sLightCtrlInitErr = CHIP_NO_ERROR;

/* Which button alias is the PHYSICAL button.
 *
 * On both Holyiot modules the button sits on P1.13, but it lands on a different
 * alias index: sw0 on the 25015 (where P1.09 is taken by I2C21 for the SHT40,
 * so the button that would come before it is missing) and sw1 on the 25008. And
 * dk_buttons_and_leds numbers buttons by alias ORDER, not by pin - so the mask
 * differs between the two boards.
 *
 * We derive it from devicetree instead of hardcoding it. With a fixed value,
 * the application on the 25015 listens on P1.08, an unpopulated pad: the LED
 * works but the button does nothing. That symptom points straight at a suspect
 * board rather than at a line of code. */
#define SW_ON_P1_13(alias)                                                                         \
	(DT_SAME_NODE(DT_GPIO_CTLR(DT_ALIAS(alias), gpios), DT_NODELABEL(gpio1)) &&                \
	 DT_GPIO_PIN(DT_ALIAS(alias), gpios) == 13)

#if defined(CONFIG_BOARD_HOLYIOT_25015) || defined(CONFIG_BOARD_HOLYIOT_25008)
#if SW_ON_P1_13(sw0)
#define APPLICATION_BUTTON_MASK DK_BTN1_MSK
#elif SW_ON_P1_13(sw1)
#define APPLICATION_BUTTON_MASK DK_BTN2_MSK
#elif SW_ON_P1_13(sw2)
#define APPLICATION_BUTTON_MASK DK_BTN3_MSK
#else
#error "Cannot find the physical button (P1.13) among the board's sw0..sw2 aliases."
#endif
#else
/* Development kits: button 2, as in the NCS light_switch sample. */
#define APPLICATION_BUTTON_MASK DK_BTN2_MSK
#endif

#ifdef CONFIG_CHIP_ICD_UAT_SUPPORT
#define UAT_BUTTON_MASK DK_BTN3_MSK
#endif

/* A safety net for the deduction above. If someone reorders the devicetree
 * aliases, the build fails here with a clear message instead of passing and
 * leaving a dead button in the field. */
#if defined(CONFIG_BOARD_HOLYIOT_25015)
BUILD_ASSERT(APPLICATION_BUTTON_MASK == DK_BTN1_MSK,
	     "on the 25015 the physical button (P1.13) is sw0, so DK_BTN1");
#elif defined(CONFIG_BOARD_HOLYIOT_25008)
BUILD_ASSERT(APPLICATION_BUTTON_MASK == DK_BTN2_MSK,
	     "on the 25008 the physical button (P1.13) is sw1, so DK_BTN2");
#endif
} /* namespace */

/* What each signal means:
 *
 *   blue, slow breathe        commissioning window open, expecting to be added
 *                             somewhere
 *   teal, two short blinks    commissioning succeeded / schedule accepted
 *   amber, one-second pulse   Identify - "this one is me"
 *   red, three blinks         something was rejected (invalid schedule)
 *
 * Nothing stays lit permanently once the device is on the network: on battery, a
 * status LED held on costs more than the radio. */
void MatterEventHandler(const chip::DeviceLayer::ChipDeviceEvent *event, intptr_t /* arg */)
{
	switch (event->Type) {
	case chip::DeviceLayer::DeviceEventType::kCHIPoBLEAdvertisingChange:
		/* We do not write the LED directly: the state goes to Automation,
		 * which decides on its own what is shown. Otherwise the permanent
		 * indicator and commissioning trample each other. */
		Automation::SetNetState(
			event->CHIPoBLEAdvertisingChange.Result == chip::DeviceLayer::kActivity_Started
				? Automation::NetState::Commissioning
				: Automation::NetState::Normal);
		break;
	case chip::DeviceLayer::DeviceEventType::kCommissioningComplete:
		Automation::SetNetState(Automation::NetState::Normal);
		StatusLed::Flash(StatusLed::kTeal, 140, 2);
		break;
	default:
		break;
	}
}

void AppTask::DimmerTriggerEventHandler()
{
	/* Button released in under 500 ms = short press. */
	if (!sWasDimmerTriggered) {
		Automation::OnButtonShortPress();
	}

	Instance().CancelTimer(Timer::Dimmer);
	Instance().CancelTimer(Timer::DimmerTrigger);
	sWasDimmerTriggered = false;
}

void AppTask::TimerEventHandler(const Timer &timerType)
{
	switch (timerType) {
	case Timer::DimmerTrigger:
		/* Button held down for more than 500 ms. */
		LOG_INF("long press");
		sWasDimmerTriggered = true;
		Automation::OnButtonLongPress();
		Instance().CancelTimer(Timer::DimmerTrigger);
		break;
	case Timer::Dimmer:
		/* Unused: dimming is driven by the schedule, not by the button. */
		break;
	default:
		break;
	}
}

/* Identify: the same amber and the same rhythm as the button in the panel.
 *
 * In the interface, the Identify button stays amber and its glow pulses over
 * one second, ease-in-out. We reproduce exactly that here - Pulse does not drop
 * to off but to a floor, so the LED stays visibly amber between pulses. If you
 * change the duration here, change @keyframes beacon in panel/index.html too;
 * otherwise "which bulb is which" looks different on screen than on the wall. */
void AppTask::IdentifyStartHandler(Identify *)
{
	Nrf::PostTask([] { StatusLed::SetOverlay(StatusLed::Pattern::Pulse, StatusLed::kAmber, 1000); });
}

void AppTask::IdentifyStopHandler(Identify *)
{
	Nrf::PostTask([] { StatusLed::ClearOverlay(); });
}

void AppTask::ButtonEventHandler(Nrf::ButtonState state, Nrf::ButtonMask hasChanged)
{
	if ((APPLICATION_BUTTON_MASK & state & hasChanged)) {
		LOG_INF("Button has been pressed, keep in this state for at least 500 ms to change light sensitivity of bound lighting devices.");
		Instance().StartTimer(Timer::DimmerTrigger, kDimmerTriggeredTimeout);
	} else if ((APPLICATION_BUTTON_MASK & hasChanged)) {
		Nrf::PostTask([] { DimmerTriggerEventHandler(); });
#ifdef CONFIG_CHIP_ICD_UAT_SUPPORT
	} else if ((UAT_BUTTON_MASK & state & hasChanged)) {
		LOG_INF("ICD UserActiveMode has been triggered.");
		Server::GetInstance().GetICDManager().OnNetworkActivity();
#endif
	}
}

void AppTask::StartTimer(Timer timer, uint32_t timeoutMs)
{
	switch (timer) {
	case Timer::DimmerTrigger:
		k_timer_start(&sDimmerPressKeyTimer, K_MSEC(timeoutMs), K_NO_WAIT);
		break;
	case Timer::Dimmer:
		k_timer_start(&sDimmerTimer, K_MSEC(timeoutMs), K_MSEC(timeoutMs));
		break;
	default:
		break;
	}
}

void AppTask::CancelTimer(Timer timer)
{
	switch (timer) {
	case Timer::DimmerTrigger:
		k_timer_stop(&sDimmerPressKeyTimer);
		break;
	case Timer::Dimmer:
		k_timer_stop(&sDimmerTimer);
		break;
	default:
		break;
	}
}

void AppTask::UserTimerTimeoutCallback(k_timer *timer)
{
	if (!timer) {
		return;
	}
	Timer timerType;

	if (timer == &sDimmerPressKeyTimer) {
		timerType = Timer::DimmerTrigger;
	} else if (timer == &sDimmerTimer) {
		timerType = Timer::Dimmer;
	} else {
		return;
	}

	Nrf::PostTask([timerType]() { TimerEventHandler(timerType); });
}

CHIP_ERROR AppTask::Init()
{
	/* Before the stack: the post-server-init callback can already signal on
	 * the LED, and that callback runs from inside PrepareServer. */
	StatusLed::Init();

	/* Initialize Matter stack */
	ReturnErrorOnFailure(Nrf::Matter::PrepareServer(Nrf::Matter::InitData{ .mPostServerInitClbk = [] {
		/* Order matters: LightCtrl needs Server::GetInstance() already
		 * initialized (fabric table + CASE session manager). */
		/* We do NOT stop the chain on the first error.
		 *
		 * The first version had ReturnErrorOnFailure here, and when
		 * LightCtrl::Init failed on the board it took everything after it
		 * down too: no schedule, no LED indicator, no lock state. A
		 * subsystem that falls over must not take the device with it - it
		 * only has to be visible that it fell over. */
		sLightCtrlInitErr = LightCtrl::Init(kLightSwitchEndpointId);
		if (sLightCtrlInitErr != CHIP_NO_ERROR) {
			LOG_ERR("LightCtrl::Init: %" CHIP_ERROR_FORMAT,
				sLightCtrlInitErr.Format());
		}
		LockState::Init();
		LockCluster::Init();
		Automation::Init();
		Battery::Init();
		return CHIP_NO_ERROR;
	} }));

	/* Initialize application timers */
	k_timer_init(&sDimmerPressKeyTimer, AppTask::UserTimerTimeoutCallback, nullptr);
	k_timer_init(&sDimmerTimer, AppTask::UserTimerTimeoutCallback, nullptr);

	if (!Nrf::GetBoard().Init(ButtonEventHandler)) {
		LOG_ERR("User interface initialization failed.");
		return CHIP_ERROR_INCORRECT_STATE;
	}

	/* Our own state handler instead of the NCS one: that one lights separate
	 * GPIO LEDs, assuming a DK with four of them. We have exactly one, and we
	 * say the state through color. */
	ReturnErrorOnFailure(Nrf::Matter::RegisterEventHandler(MatterEventHandler, 0));

	return Nrf::Matter::StartServer();
}

CHIP_ERROR AppTask::StartApp()
{
	ReturnErrorOnFailure(Init());

	while (true) {
		Nrf::DispatchNextTask();
	}

	return CHIP_NO_ERROR;
}
