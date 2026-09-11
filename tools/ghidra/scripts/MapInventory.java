// Полная опись карт: дизассемблировать весь код, создать функции по целям
// вызовов, декомпилировать всё и выписать вызовы процедур интерполяции
// вместе с константными аргументами.
//
// Аргумент: путь .txt для результата.
//
//@category Firmware
import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.symbol.Reference;

import java.io.PrintWriter;
import java.util.*;
import java.util.regex.*;

public class MapInventory extends GhidraScript {

    private static final long BASE = 0x800000L;
    private static final long CODE_LO = 0x20000L, CODE_HI = 0x6C000L;

    private Address at(long off) {
        return currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(BASE + off);
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String out = args.length > 0 ? args[0] : null;

        println("дизассемблирую весь код...");
        AddressSet set = new AddressSet(at(CODE_LO), at(CODE_HI - 1));
        new DisassembleCommand(set, null, true).applyTo(currentProgram, monitor);

        // функции по целям вызовов
        Set<Address> callees = new TreeSet<>();
        InstructionIterator it = currentProgram.getListing().getInstructions(set, true);
        while (it.hasNext() && !monitor.isCancelled()) {
            Instruction ins = it.next();
            String m = ins.getMnemonicString();
            if (!m.startsWith("call")) continue;
            for (Reference r : ins.getReferencesFrom()) {
                if (r.getReferenceType().isCall()) callees.add(r.getToAddress());
            }
        }
        println("целей вызовов: " + callees.size());
        int made = 0;
        for (Address a : callees) {
            if (currentProgram.getFunctionManager().getFunctionContaining(a) == null) {
                if (new CreateFunctionCmd(a).applyTo(currentProgram, monitor)) made++;
            }
        }
        println("создано функций: " + made);

        DecompInterface dec = new DecompInterface();
        dec.openProgram(currentProgram);

        Pattern call = Pattern.compile("func_0x([0-9a-f]+)\\(([^;]*?)\\)");
        Map<String, Integer> freq = new TreeMap<>();
        List<String> rows = new ArrayList<>();

        FunctionIterator fit = currentProgram.getFunctionManager().getFunctions(true);
        int done = 0, total = currentProgram.getFunctionManager().getFunctionCount();
        while (fit.hasNext() && !monitor.isCancelled()) {
            Function f = fit.next();
            done++;
            if (done % 250 == 0) println("  декомпилировано " + done + " из " + total);
            DecompileResults res = dec.decompileFunction(f, 45, monitor);
            if (!res.decompileCompleted()) continue;
            String c = res.getDecompiledFunction().getC();
            Matcher mm = call.matcher(c);
            while (mm.find()) {
                String target = mm.group(1);
                String argv = mm.group(2).replaceAll("\\s+", "");
                freq.merge(target, 1, Integer::sum);
                if (argv.matches("0x[0-9a-f]+,0x2[0-9a-f]+(,0x[0-9a-f]+,0x2[0-9a-f]+)?")) {
                    rows.add(String.format("%s\tfunc_0x%s\t%s", f.getEntryPoint(), target, argv));
                }
            }
        }

        StringBuilder sb = new StringBuilder();
        sb.append("# частота вызовов подпрограмм (кандидаты в интерполяторы сверху)\n");
        List<Map.Entry<String, Integer>> fl = new ArrayList<>(freq.entrySet());
        fl.sort((x, y) -> y.getValue() - x.getValue());
        for (int i = 0; i < Math.min(25, fl.size()); i++) {
            sb.append(String.format("  func_0x%s  %d\n", fl.get(i).getKey(), fl.get(i).getValue()));
        }
        sb.append("\n# вызовы с константами вида (указатель, страница[, ось, страница])\n");
        sb.append("# функция\tподпрограмма\tаргументы\n");
        Collections.sort(rows);
        for (String r : rows) sb.append(r).append("\n");

        println("строк с константами: " + rows.size());
        if (out != null) {
            PrintWriter pw = new PrintWriter(out, "UTF-8");
            pw.print(sb);
            pw.close();
            println("записано " + out);
        }
    }
}
