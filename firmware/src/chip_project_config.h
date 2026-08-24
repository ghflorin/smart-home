/*
 * Copyright (c) 2022 Nordic Semiconductor ASA
 *
 * SPDX-License-Identifier: LicenseRef-Nordic-5-Clause
 */

#pragma once

#define CHIP_CONFIG_CONTROLLER_MAX_ACTIVE_DEVICES 2
/* One dynamic endpoint: the lock endpoint, declared in src/lock_cluster.cpp.
 * Without this, MAX_ENDPOINT_COUNT stays equal to the number of fixed endpoints
 * and emberAfSetDynamicEndpoint has nowhere to put it. */
#define CHIP_DEVICE_CONFIG_DYNAMIC_ENDPOINT_COUNT 1


