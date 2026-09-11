// Опись карт по вызовам процедур интерполяции.
//
// Проходит по всему дизассемблированному коду, ловит calls на заданные
// адреса и собирает непосредственные значения, положенные в r12..r15
// непосредственно перед вызовом. Это и есть указатель на тело карты,
// его страница, указатель на ось и её страница.
//
// Аргументы: цели вызовов через запятую (например 833ffc,834310) и путь .txt
//
//@category Firmware
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;

import java.io.PrintWriter;
import java.util.*;

public class InterpCalls extends GhidraScript {

    private static final long BASE = 0x800000L;
    private static final long CODE_LO = 0x20000L, CODE_HI = 0x6C000L;

    private Address at(long off) {
        return currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(BASE + off);
    }

    private static boolean regsEmpty(Map<String, String> regs) {
        return !regs.containsKey("r12") && !regs.containsKey("r14");
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        Set<String> targets = new HashSet<>(Arrays.asList(args[0].toLowerCase().split(",")));
        String out = args.length > 1 ? args[1] : null;

        AddressSet set = new AddressSet(at(CODE_LO), at(CODE_HI - 1));
        if (currentProgram.getListing().getInstructions(set, true).hasNext() == false) {
            println("код не разобран, дизассемблирую...");
            new ghidra.app.cmd.disassemble.DisassembleCommand(set, null, true)
                    .applyTo(currentProgram, monitor);
        }
        List<Instruction> win = new ArrayList<>();
        List<String> rows = new ArrayList<>();
        Map<String, Integer> hits = new TreeMap<>();

        InstructionIterator it = currentProgram.getListing().getInstructions(set, true);
        while (it.hasNext() && !monitor.isCancelled()) {
            Instruction ins = it.next();
            win.add(ins);
            if (win.size() > 16) win.remove(0);
            if (!ins.getMnemonicString().startsWith("call")) continue;
            String txt = ins.toString().toLowerCase().replace("0x", "");
            String tgt = null;
            if (targets.contains("*")) {
                int sp = txt.indexOf(' ');
                tgt = sp > 0 ? txt.substring(sp + 1).trim() : "?";
            } else {
                for (String t : targets) if (txt.contains(t)) tgt = t;
            }
            if (tgt == null) continue;
            hits.merge(tgt, 1, Integer::sum);

            Map<String, String> regs = new LinkedHashMap<>();
            for (int i = win.size() - 2; i >= 0; i--) {
                String s = win.get(i).toString().toLowerCase();
                if (s.startsWith("calls") || s.startsWith("rets")) break;
                for (String r : new String[]{"r12", "r13", "r14", "r15"}) {
                    if (s.startsWith("mov " + r + ",#") && !regs.containsKey(r)) {
                        regs.put(r, s.substring(s.indexOf('#') + 1));
                    }
                }
            }
            if (targets.contains("*") && regsEmpty(regs)) continue;
            rows.add(String.format("%s\t%s\tr12=%s r13=%s r14=%s r15=%s",
                    ins.getAddress(), tgt,
                    regs.getOrDefault("r12", "-"), regs.getOrDefault("r13", "-"),
                    regs.getOrDefault("r14", "-"), regs.getOrDefault("r15", "-")));
        }

        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, Integer> e : hits.entrySet())
            sb.append(String.format("# %s: %d вызовов\n", e.getKey(), e.getValue()));
        sb.append("# адрес вызова\tпроцедура\tаргументы\n");
        for (String r : rows) sb.append(r).append("\n");
        println("вызовов найдено: " + rows.size());
        if (out != null) {
            PrintWriter pw = new PrintWriter(out, "UTF-8");
            pw.print(sb); pw.close();
            println("записано " + out);
        }
    }
}
