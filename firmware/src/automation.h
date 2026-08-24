#pragma once

#include <cstdint>

namespace Automation {

void Init(void);
void OnButtonShortPress(void);
void OnButtonLongPress(void);

/* Network state, as the indicator sees it. Set by the Matter event handler in
 * app_task.cpp. */
enum class NetState : uint8_t {
	Commissioning, /* window open, waiting to be added */
	Normal,        /* on the network, or at least not commissioning */
};

void SetNetState(NetState s);

/* Recomputes what the LED shows: the color and brightness of the level the
 * switch would apply right now, or fully dark if it is locked. Called when
 * something that matters changes (lock, role, schedule, time of day), not
 * periodically. */
void RefreshIndicator(void);

} /* namespace Automation */
