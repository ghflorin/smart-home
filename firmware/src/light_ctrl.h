/*
 * light_ctrl - sends Matter commands straight to the bound bulbs (no hub).
 *
 * Everything here must be called from the Matter thread (CHIP stack lock held).
 * From other threads use LightCtrl::PostFromISR / SystemLayer ScheduleLambda.
 */
#pragma once

#include <cstdint>
#include <app-common/zap-generated/cluster-objects.h>
#include <app/clusters/bindings/BindingManager.h>

namespace LightCtrl {

/* The local endpoint hosting the client clusters (OnOff + LevelControl). It has
 * to match the application's ZAP. In the light_switch sample it is 1. Set
 * through Init(). */
chip::EndpointId SwitchEndpoint(void);

enum class Action : uint8_t {
	Off,
	On,
	Toggle,
	/* Sends MoveToLevelWithOnOff - turns the bulb on AND sets the brightness
	 * in a single command, so there is no flash at full brightness. */
	SetLevel,
	/* Color temperature, in mireds (1,000,000 / kelvin).
	 * Bulbs without ColorControl simply have no binding entry for this
	 * cluster, so the command never reaches them. The capability lives in the
	 * binding table, not in the firmware. */
	SetColorTemp,
	/* Persistent attribute writes into the bulb. Kept separate rather than
	 * combined: each write has its own callback and its own context
	 * lifetime, and the two attributes live on different clusters. */
	WriteStartupOnOff,
	WriteStartupLevel,
	/* Writes the lock state to the other SWITCHES in the binding table, not
	 * to bulbs. Used only by the switch in the lock role. The binding entries
	 * for this use cluster 0xFFF1FC30 and endpoint 2. */
	WriteLock,
};

struct Request {
	Action action;
	uint8_t level;          /* SetLevel: 1..254. WriteStartupState: see below. */
	uint16_t transitionDs;  /* transition time in deciseconds (10 = 1s) */
	uint8_t startupOnOff;   /* WriteStartupState only: 0/1/2, or 0xFF = null */
	uint16_t mireds;        /* SetColorTemp only */
	bool locked;            /* WriteLock only */
};

/* Init: registers the handler on BindingManager. Call once, after the Matter
 * stack has started. */
CHIP_ERROR Init(chip::EndpointId switchEndpoint);

/* Sends to ALL nodes in the endpoint's binding table.
 * Thread-safe: may be called from any thread. */
void Post(const Request &req);

/* Convenience helpers. */
void On(void);
void Off(void);
void Toggle(void);
void SetLevel(uint8_t level, uint16_t transitionDs = 5);

/* Color temperature. 454 mireds ~ 2200K (warm), 250 ~ 4000K, 153 ~ 6500K
 * (cool). The bulb clamps it to its own physical range. */
void SetColorTemp(uint16_t mireds, uint16_t transitionDs = 5);

/* Writes the power-loss recovery state into every bound bulb.
 *
 * onOff:
 *   0 = stay off, 1 = turn on, 2 = toggle relative to the previous state,
 *   255 = null -> the bulb returns to its state from before the outage.
 * level:
 *   0x00 = MinLevel, 0x01..0xFE = that value,
 *   0xFF = return to the level from before the outage.
 *
 * NOTE: writing these attributes requires Manage privilege in the bulb's ACL,
 * not just Operate. See scripts/commission.sh. */
void WriteStartupState(uint8_t onOff, uint8_t level);

/* Sends the lock state to every switch in the binding table.
 *
 * It sends the VALUE, not a toggle: if a switch misses the command, it
 * resynchronizes on the next press instead of staying inverted forever. */
void WriteLock(bool locked);

} /* namespace LightCtrl */
