#pragma once

/*
 * The button, reported as a button.
 *
 * The switch commands its bulbs itself, over Thread, bound device to device -
 * that is the point, and why the wall still works with the Pi unplugged. This
 * cluster changes none of that. It only says out loud that a finger arrived, so
 * anything listening can act on it too.
 *
 * See switch_cluster.cpp for why it is a dynamic endpoint and what the panel
 * does with the events.
 */
namespace SwitchCluster {

/* Adds the endpoint. Safe before the network is up; nothing is sent here. */
void Init(void);

/* The finger went down. */
void Pressed(void);

/* Still down past the long-press threshold. */
void LongHeld(void);

/* And up again. `wasLong` decides which release this was. */
void Released(bool wasLong);

} /* namespace SwitchCluster */
