/*
 * fault - the reason a switch fell over, kept across the restart.
 *
 * A fault restarts the switch, and on a coin cell every restart is a network
 * join, a new session with the hub and a full report. With no console nobody
 * ever saw why: the panel counted thousands of restarts on one switch and had
 * nothing to say about any of them. So the fatal error handler leaves the reason
 * and the address in RAM that survives a soft reset, and the next boot
 * publishes them.
 *
 * WHY .noinit IS SAFE HERE. MCUboot runs between the reset and us, and it keeps
 * to the bottom 22 KB of RAM (its datas/bss/noinit end at 0x200054f0) and does
 * not clear the rest (MCUBOOT_CLEANUP_RAM is off). Our .noinit starts at
 * 0x20020c80. After a power-on the bytes are noise, which is what the magic is
 * for.
 *
 * THE HANDLER REPLACES NCS's. CONFIG_RESET_ON_FATAL_ERROR provided one that only
 * reset; it is off in prj.conf, and this one writes the record and then resets
 * the same way.
 */
#include "fault.h"

#include <zephyr/arch/cpu.h>
#include <zephyr/fatal.h>
#include <zephyr/kernel.h>
#include <zephyr/linker/section_tags.h>

extern "C" void sys_arch_reboot(int type);

namespace {

constexpr uint32_t kMagic = 0x46415554; /* "FAUT" */

struct Record {
	uint32_t magic;
	Fault::Info info;
};

__noinit Record sRecord;
Fault::Info sLast = { Fault::kNone, 0, 0 };

} /* namespace */

extern "C" void k_sys_fatal_error_handler(unsigned int reason, const struct arch_esf *esf)
{
	sRecord.info = { reason, esf ? esf->basic.pc : 0, esf ? esf->basic.lr : 0 };
	sRecord.magic = kMagic;
	sys_arch_reboot(0);
	CODE_UNREACHABLE;
}

namespace Fault {

void Init(void)
{
	if (sRecord.magic == kMagic) {
		sLast = sRecord.info;
	}
	sRecord.magic = 0;
}

const Info &Last(void)
{
	return sLast;
}

} /* namespace Fault */
