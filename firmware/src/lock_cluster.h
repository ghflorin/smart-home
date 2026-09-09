/*
 * Our own cluster, on dynamic endpoint 2.
 *
 * It exposes two attributes, both about locking the switch:
 *   0xFFF10002  Locked  (boolean)  the switch can only turn a light off
 *   0xFFF10003  Role    (uint8)    0 = light, 1 = lock
 *
 * Until the schedule moved to the Raspberry Pi, it was also the schedule's
 * storage (a blob plus a revision counter). The switch no longer runs the
 * schedule - the Pi writes straight into the bulbs' persistent attributes - so
 * keeping it here would have meant syncing data nobody uses to a battery
 * device.
 */
#pragma once

namespace LockCluster {

/* Registers the dynamic endpoint. Called from app_task, after the Matter server
 * has been initialized. */
void Init(void);

/* Tells the fabric that Locked changed. A write from the hub is reported by the
 * stack itself; a press on the switch is not, and the panel only ever learned
 * that a lock had been pressed by noticing its targets change. Call it after
 * LockState::SetLocked on the button path. Safe from any thread. */
void ReportLocked(void);

} /* namespace LockCluster */
