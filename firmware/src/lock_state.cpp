#include "lock_state.h"

#include "automation.h"

#include <platform/KeyValueStoreManager.h>

#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(lock_state, CONFIG_SMARTHOME_LOG_LEVEL);

using namespace chip;

namespace {

constexpr char kLockedKey[] = "rr/locked";
constexpr char kRoleKey[] = "rr/role";

bool sLocked;
LockState::Role sRole = LockState::Role::Light;

template <typename T> void Load(const char *key, T *out)
{
	size_t len = 0;
	uint8_t raw = 0;
	if (DeviceLayer::PersistedStorage::KeyValueStoreMgr().Get(key, &raw, sizeof(raw), &len) ==
		    CHIP_NO_ERROR &&
	    len == sizeof(raw)) {
		*out = static_cast<T>(raw);
	}
}

void Store(const char *key, uint8_t value)
{
	DeviceLayer::PersistedStorage::KeyValueStoreMgr().Put(key, &value, sizeof(value));
}

} /* namespace */

namespace LockState {

void Init(void)
{
	uint8_t locked = 0;
	Load(kLockedKey, &locked);
	sLocked = locked != 0;

	uint8_t role = 0;
	Load(kRoleKey, &role);
	sRole = (role == 1) ? Role::Lock : Role::Light;

	LOG_INF("initial state: %s, role %s", sLocked ? "locked" : "unlocked",
		sRole == Role::Lock ? "lock" : "light");
}

bool IsLocked(void)
{
	return sLocked;
}

void SetLocked(bool locked, bool persist)
{
	if (sLocked == locked && persist) {
		/* A write that changes nothing: do not wear out storage for
		 * nothing. The panel may resend the same value on every page
		 * load. */
		return;
	}
	sLocked = locked;
	if (persist) {
		Store(kLockedKey, locked ? 1 : 0);
	}
	LOG_INF("%s", locked ? "locked" : "unlocked");

	/* The LED is the only hint that the switch is dead on purpose rather than
	 * broken, so it has to change immediately, not on the next tick. */
	Automation::RefreshIndicator();
}

Role GetRole(void)
{
	return sRole;
}

void SetRole(Role role, bool persist)
{
	sRole = role;
	if (persist) {
		Store(kRoleKey, static_cast<uint8_t>(role));
	}
	LOG_INF("new role: %s", role == Role::Lock ? "lock" : "light");
	Automation::RefreshIndicator();
}

} /* namespace LockState */
