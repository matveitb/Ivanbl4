// Дизассемблировать код вокруг заданных адресов и выдать декомпиляцию.
//
// Аргументы: шестнадцатеричные смещения в файле (без базы) и последним --
// путь .txt для сохранения. База образа 0x800000.
//
//@category Firmware
import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.Listing;

import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;

public class DecompileTargets extends GhidraScript {

    private static final long BASE = 0x800000L;

    private Address at(long off) {
        return currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(BASE + off);
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String out = null;
        List<Long> targets = new ArrayList<>();
        for (String a : args) {
            if (a.endsWith(".txt")) { out = a; continue; }
            targets.add(Long.parseLong(a.replace("0x", ""), 16));
        }

        Listing listing = currentProgram.getListing();
        for (long off : targets) {
            new DisassembleCommand(at(off), null, true).applyTo(currentProgram, monitor);
        }
        for (long off : targets) {
            if (currentProgram.getFunctionManager().getFunctionContaining(at(off)) == null) {
                new CreateFunctionCmd(at(off)).applyTo(currentProgram, monitor);
            }
        }

        DecompInterface dec = new DecompInterface();
        dec.openProgram(currentProgram);

        StringBuilder sb = new StringBuilder();
        for (long off : targets) {
            Address a = at(off);
            sb.append("======================================================================\n");
            sb.append(String.format("0x%05X  ", off));
            Function f = currentProgram.getFunctionManager().getFunctionContaining(a);
            if (f == null) {
                sb.append("функция не определена, листинг:\n");
                Instruction ins = listing.getInstructionAt(a);
                for (int i = 0; ins != null && i < 60; i++) {
                    sb.append(String.format("  %s  %s%n", ins.getAddress(), ins));
                    ins = ins.getNext();
                }
                sb.append("\n");
                continue;
            }
            sb.append(String.format("функция %s @ %s, размер %d%n",
                    f.getName(), f.getEntryPoint(), f.getBody().getNumAddresses()));
            DecompileResults res = dec.decompileFunction(f, 180, monitor);
            if (res.decompileCompleted()) {
                sb.append(res.getDecompiledFunction().getC());
            } else {
                sb.append("  декомпиляция не удалась: ").append(res.getErrorMessage()).append("\n");
            }
            sb.append("\n");
        }

        println(sb.toString());
        if (out != null) {
            PrintWriter pw = new PrintWriter(out, "UTF-8");
            pw.print(sb);
            pw.close();
            println("записано " + out);
        }
    }
}
