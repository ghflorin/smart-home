#pragma once

#include <cstdint>

/*
 * What is left in the cell, reported through Matter's PowerSource cluster.
 *
 * There is no divider on the board and none was added: the supply is bracketed
 * with the power-fail comparator, which costs a few register writes and nothing
 * at all when idle. The nRF54L's ADC cannot see VDD - see battery.cpp.
 */
namespace Battery {

/* Adds the PowerSource endpoint, then measures every hour starting half a
 * minute after boot. */
void Init(void);

/* The last reading, in millivolts, as a LOWER BOUND - 2700 means "at least
 * 2.7 V". -1 before the first reading, and also when the supply is under the
 * comparator's lowest step. */
int32_t LastMillivolts(void);

} /* namespace Battery */
