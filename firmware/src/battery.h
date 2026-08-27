#pragma once

#include <cstdint>

/*
 * What is left in the cell, reported through Matter's PowerSource cluster.
 *
 * There is no divider on the board and none was added: the nRF54L's ADC reads
 * its own analogue supply, so the measurement costs a few microseconds of ADC
 * and nothing at all when idle. See the `&adc` block in the board overlays.
 */
namespace Battery {

/* Sets up the ADC, publishes a first reading, then repeats hourly. Safe to call
 * when the ADC is missing from devicetree - it logs and does nothing further,
 * so a board without the overlay still boots. */
void Init(void);

/* The last voltage read, in millivolts, or -1 before the first reading. */
int32_t LastMillivolts(void);

} /* namespace Battery */
