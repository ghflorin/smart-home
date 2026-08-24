#include "lock_cluster.h"

#include "lock_state.h"

#include <app-common/zap-generated/ids/Clusters.h>
#include <app/util/attribute-storage.h>

#include <cstring>

#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(lock_cluster, CONFIG_SMARTHOME_LOG_LEVEL);

using namespace chip;
using namespace chip::app;
using chip::Protocols::InteractionModel::Status;

namespace {

/* Why a DYNAMIC endpoint rather than one from ZAP.
 *
 * A vendor cluster on a fixed endpoint requires regenerating the whole
 * app-common from the SDK - several megabytes of generated code, vendored into
 * the project and resynced on every NCS update. Not worth it for two
 * attributes. A dynamic endpoint is declared in C++ with raw identifiers and
 * does not touch the generated data model at all.
 *
 * The price: one extra endpoint shows up in the Descriptor. It has no device
 * type any ecosystem would recognize, so it should be ignored - but "should" is
 * all I can claim until I see it in Apple Home on real hardware. */
constexpr ClusterId kClusterId = 0xFFF1FC30; /* test vendor 0xFFF1.
					      * NOTE: 0xFFF1FC05/06/20 are
					      * already taken by the SDK -
					      * 0x...20 is the Sample MEI
					      * cluster. */
constexpr AttributeId kLockedAttr = 0xFFF10002;
constexpr AttributeId kRoleAttr = 0xFFF10003;
constexpr EndpointId kEndpointId = 2;

/* DeviceTypeId is uint32_t in the SDK (lib/core/DataModelTypes.h). Declared
 * wrongly as uint16_t, 0xFFF10001 truncated to 0x0001 - i.e. the endpoint
 * reported a STANDARD device type from the SIG space instead of a vendor one.
 * Exactly backwards from the intent: a standard type is far more likely to be
 * acted on by an ecosystem. The composition is read once, at commissioning, so
 * the mistake would have stayed baked into the fabric. */
constexpr chip::DeviceTypeId kDeviceType = 0xFFF10001;

DECLARE_DYNAMIC_ATTRIBUTE_LIST_BEGIN(sLockAttrs)
DECLARE_DYNAMIC_ATTRIBUTE(kLockedAttr, BOOLEAN, 1, ZAP_ATTRIBUTE_MASK(WRITABLE)),
	DECLARE_DYNAMIC_ATTRIBUTE(kRoleAttr, INT8U, 1, ZAP_ATTRIBUTE_MASK(WRITABLE)),
	DECLARE_DYNAMIC_ATTRIBUTE_LIST_END();

/* Descriptor is mandatory on every endpoint. Its attributes are served by the
 * SDK through a global AttributeAccessInterface, so declaring the cluster is
 * enough - we do not have to answer for them ourselves. */
DECLARE_DYNAMIC_ATTRIBUTE_LIST_BEGIN(sDescriptorAttrs)
DECLARE_DYNAMIC_ATTRIBUTE(Clusters::Descriptor::Attributes::DeviceTypeList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(Clusters::Descriptor::Attributes::ServerList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(Clusters::Descriptor::Attributes::ClientList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE(Clusters::Descriptor::Attributes::PartsList::Id, ARRAY, 254, 0),
	DECLARE_DYNAMIC_ATTRIBUTE_LIST_END();

DECLARE_DYNAMIC_CLUSTER_LIST_BEGIN(sClusters)
DECLARE_DYNAMIC_CLUSTER(kClusterId, sLockAttrs, ZAP_CLUSTER_MASK(SERVER), nullptr, nullptr),
	DECLARE_DYNAMIC_CLUSTER(Clusters::Descriptor::Id, sDescriptorAttrs, ZAP_CLUSTER_MASK(SERVER), nullptr,
				nullptr) DECLARE_DYNAMIC_CLUSTER_LIST_END;

DECLARE_DYNAMIC_ENDPOINT(sEndpoint, sClusters);
DataVersion sDataVersions[ArraySize(sClusters)];
const EmberAfDeviceType sDeviceTypes[] = { { kDeviceType, 1 } };

} /* namespace */

namespace LockCluster {

void Init(void)
{
	CHIP_ERROR err = emberAfSetDynamicEndpoint(0, kEndpointId, &sEndpoint,
						   Span<DataVersion>(sDataVersions),
						   Span<const EmberAfDeviceType>(sDeviceTypes));
	if (err != CHIP_NO_ERROR) {
		LOG_ERR("cannot add the lock endpoint: %s", ErrorStr(err));
	}
}

} /* namespace LockCluster */

/* A dynamic endpoint has no attribute storage of its own: every read and write
 * goes through these callbacks. The real state lives in LockState, which also
 * persists it - which is why nothing is kept locally here. */
Status emberAfExternalAttributeReadCallback(EndpointId endpoint, ClusterId clusterId,
					    const EmberAfAttributeMetadata *metadata, uint8_t *buffer,
					    uint16_t maxReadLength)
{
	if (clusterId != 0xFFF1FC30 || !metadata || !buffer) {
		return Status::Failure;
	}
	if (maxReadLength < 1) {
		return Status::ResourceExhausted;
	}

	if (metadata->attributeId == 0xFFF10002) {
		buffer[0] = LockState::IsLocked() ? 1 : 0;
		return Status::Success;
	}
	if (metadata->attributeId == 0xFFF10003) {
		buffer[0] = static_cast<uint8_t>(LockState::GetRole());
		return Status::Success;
	}
	return Status::UnsupportedAttribute;
}

Status emberAfExternalAttributeWriteCallback(EndpointId endpoint, ClusterId clusterId,
					     const EmberAfAttributeMetadata *metadata, uint8_t *buffer)
{
	if (clusterId != 0xFFF1FC30 || !metadata || !buffer) {
		return Status::Failure;
	}
	if (metadata->attributeId == 0xFFF10002) {
		LockState::SetLocked(buffer[0] != 0, true);
		return Status::Success;
	}
	if (metadata->attributeId == 0xFFF10003) {
		if (buffer[0] > 1) {
			return Status::InvalidValue;
		}
		LockState::SetRole(static_cast<LockState::Role>(buffer[0]), true);
		return Status::Success;
	}
	return Status::UnsupportedWrite;
}
