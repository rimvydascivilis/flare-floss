# Copyright (C) 2017 FireEye, Inc. All Rights Reserved.

import re
import logging

from collections import namedtuple

import viv_utils
import envi.archs.i386
import envi.archs.amd64
import viv_utils.emulator_drivers

import strings
from utils import makeEmulator


logger = logging.getLogger(__name__)
MAX_STACK_SIZE = 0x10000

MIN_NUMBER_OF_MOVS = 5

CallContext = namedtuple("CallContext",
                         [
                             "pc",  # the current program counter, type: int
                             "sp",  # the current stack counter, type: int
                             "init_sp",   # the initial stack counter at start of function, type: int
                             "stack_memory",  # the active stack frame contents, type: str
                         ])


class StackstringContextMonitor(viv_utils.emulator_drivers.Monitor):
    """
    Observes emulation and extracts the active stack frame contents:
    - at each function call in a function
    - based on a heuristic looking for mov instructions to consecutive memory addresses
    """

    def __init__(self, vw, init_sp, bb_ends):
        viv_utils.emulator_drivers.Monitor.__init__(self, vw)

        # this is a public field
        # type: List[CallContext]
        self.ctxs = []

        # this is a private field
        self._init_sp = init_sp

        self.bb_ends = bb_ends  # index of VAs of the last instruction of all basic blocks
        self.mov_count = 0  # count of stack mov instructions in current basic block

    def apicall(self, emu, op, pc, api, argv):
        self.extract_context(emu, op)

    def extract_context(self, emu, op):
        """ Extract only the bytes on the stack between the base pointer specifically, stack pointer at function entry),
        and stack pointer. """
        stack_top = emu.getStackCounter()
        stack_bottom = self._init_sp
        stack_size = stack_bottom - stack_top
        if stack_size > MAX_STACK_SIZE:
            logger.debug('stack size too big: 0x%x', stack_size)
            return

        stack_buf = emu.readMemory(stack_top, stack_size)
        self.ctxs.append(CallContext(op.va, stack_top, stack_bottom, stack_buf))

    def posthook(self, emu, op, endpc):
        self.get_context_via_mov_heuristic(emu, op, endpc)

    def get_context_via_mov_heuristic(self, emu, op, endpc):
        """ Extract contexts at end of a basic block (bb) if bb contains enough movs to stack memory. """
        # TODO check number of written bytes?
        # count movs, shortcut if this basic block has enough writes to trigger context extraction already
        if self.mov_count < MIN_NUMBER_OF_MOVS and self.is_stack_mov(emu, op):
            self.mov_count += 1
        if endpc in self.bb_ends:
            if self.mov_count >= MIN_NUMBER_OF_MOVS:
                self.extract_context(emu, op)
            # reset counter at end of basic block
            self.mov_count = 0

    def is_stack_mov(self, emu, op):
        if op.mnem[:3] == "mov":
            if not op.getOperands():
                # no operands, e.g. movsb, movsd, fail safe and count these regardless of where data is moved to
                return True
            else:
                return isinstance(op.getOperands()[0], envi.archs.i386.disasm.i386SibOper)


def extract_call_contexts(vw, fva, bb_ends):
    emu = makeEmulator(vw)
    monitor = StackstringContextMonitor(vw, emu.getStackCounter(), bb_ends)
    driver = viv_utils.emulator_drivers.FunctionRunnerEmulatorDriver(emu)
    driver.add_monitor(monitor)
    driver.runFunction(fva, maxhit=1, maxrep=0x100, func_only=True)
    return monitor.ctxs


# StackString represents a stackstring extracted from a function.
StackString = namedtuple("StackString",
                         [
                             # type: int
                             # the address from which the stackstring was extracted.
                             "fva",

                             # type: str
                             # the string contents.
                             "s",

                             # type: int
                             # the program counter at which the stackstring existed.
                             "pc",

                             # here's what the following members represent...
                             #
                             #
                             # [smaller addresses]
                             #
                             # +---------------+  <- sp (top of stack)
                             # |               | \
                             # +---------------+  | offset
                             # |               | /
                             # +---------------+
                             # | "abc"         | \
                             # +---------------+  |
                             # |               |  |
                             # +---------------+  | frame_offset
                             # |               |  |
                             # +---------------+  |
                             # |               | /
                             # +---------------+  <- init_sp (bottom of stack, probably bp)
                             #
                             # [bigger addresses]

                             # type: int
                             # the stack counter at which the stackstring existed.
                             # aka, the top of the stack frame
                             "sp",

                             # type: int
                             # the initial stack counter at the start of the function.
                             # aka, the bottom of the stack frame
                             "init_sp",

                             # type: int
                             # the offset into the stack frame at which the stackstring existed.
                             "offset",

                             # type: int
                             # the offset from the function frame at which the stackstring existed.
                             "frame_offset",
                         ])


def getPointerSize(vw):
    if isinstance(vw.arch, envi.archs.amd64.Amd64Module):
        return 8
    elif isinstance(vw.arch, envi.archs.i386.i386Module):
        return 4
    else:
        raise NotImplementedError("unexpected architecture: %s" % (vw.arch.__class__.__name__))


FP_FILTER = re.compile("^p?V?A+$")
FP_FILTER_SUB = re.compile("^p?VA")  # remove string prefixes: pVA, VA


def extract_stackstrings(vw, selected_functions, bb_ends):
    '''
    Extracts the stackstrings from functions in the given workspace.

    :param vw: The vivisect workspace from which to extract stackstrings.
    :rtype: Generator[StackString]
    '''
    logger.debug('extracting stackstrings from %d functions', len(selected_functions))
    for fva in selected_functions:
        logger.debug('extracting stackstrings from function: 0x%x', fva)
        seen = set([])
        for ctx in extract_call_contexts(vw, fva, bb_ends):
            logger.debug('extracting stackstrings at checkpoint: 0x%x stacksize: 0x%x', ctx.pc, ctx.init_sp - ctx.sp)
            for s in strings.extract_ascii_strings(ctx.stack_memory):
                if FP_FILTER.match(s.s):
                    # ignore strings like: pVA, pVAAA, AAAA
                    # which come from vivisect uninitialized taint tracking
                    continue
                s_stripped = re.sub(FP_FILTER_SUB, "", s.s)
                if s_stripped not in seen:
                    frame_offset = (ctx.init_sp - ctx.sp) - s.offset - getPointerSize(vw)
                    yield(StackString(fva, s_stripped, ctx.pc, ctx.sp, ctx.init_sp, s.offset, frame_offset))
                    seen.add(s_stripped)
            for s in strings.extract_unicode_strings(ctx.stack_memory):
                if FP_FILTER.match(s.s):
                    # ignore strings like: pVA, pVAAA, AAAA
                    # which come from vivisect uninitialized taint tracking
                    continue
                s_stripped = re.sub(FP_FILTER_SUB, "", s.s)
                if s_stripped not in seen:
                    frame_offset = (ctx.init_sp - ctx.sp) - s.offset - getPointerSize(vw)
                    yield(StackString(fva, s_stripped, ctx.pc, ctx.sp, ctx.init_sp, s.offset, frame_offset))
                    seen.add(s_stripped)


def get_basic_block_end_index(vw):
    """
    Return the set of VAs that are the last instructions of basic blocks.
    """
    index = set([])
    for funcva in vw.getFunctions():
        f = viv_utils.Function(vw, funcva)
        for bb in f.basic_blocks:
            if len(bb.instructions) == 0:
                continue
            index.add(bb.instructions[-1].va)
    return index
