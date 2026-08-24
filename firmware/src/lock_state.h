/*
 * lock_state - the locked switch, and its role in the house.
 *
 * Two small pieces of state, but both have to survive a power outage: if you
 * locked the switches and the power drops, you do not want them to unlock
 * themselves. So they are persisted in the same storage the schedule used.
 *
 * They are written over Matter, on endpoint 2, in the same vendor cluster the
 * schedule used - see lock_cluster.cpp, which owns that endpoint.
 */
#pragma once

#include <cstdint>

namespace LockState {

enum class Role : uint8_t {
	/* Commands the bulbs in its binding table. The normal case. */
	Light = 0,
	/* Commands no bulbs. On a press it sends its own lock state to every node
	 * in its binding table. It is the "enough playing with the lights"
	 * switch.
	 *
	 * The role is runtime state, not a separate firmware: the same binary runs
	 * on any module, and the panel decides which one does what. Otherwise you
	 * would have to remember which board carries which image. */
	Lock = 1,
};

void Init(void);

bool IsLocked(void);
/* persist=false only at boot, when the value already came out of storage. */
void SetLocked(bool locked, bool persist);

Role GetRole(void);
void SetRole(Role role, bool persist);

} /* namespace LockState */
