#!/usr/bin/env python3
"""
categories -- в какую смысловую группу класть карту.

Группы те же, что видит человек в CTP7 или в описании Bosch: зажигание,
детонация, состав смеси, воздух и наполнение и так далее, а не «как я её
нашёл» (опись, сканер, профиль).

Откуда берётся группа, по порядку:

1. ЯВНЫЙ СПИСОК -- для записей, которые мы назвали сами (KST_COLD,
   HFM_LIN, TMOT_FAN1 ...) и для тех, где функция дамоса врёт про наш мотор.
2. ОСЬ -- имя вида S..##..UB / UW / SB / SW: опорные точки, своя группа.
3. ФУНКЦИЯ BOSCH -- в дамосе px5ns03d у каждой функции (/FKT, например
   ZWGRU «Basic ignition angle») есть список её калибровок. Функция и
   определяет группу. Если переменная числится в нескольких функциях,
   берётся первая не диагностическая.
4. РАЗДЕЛ ПРОФИЛЯ -- для записей профиля без имени из дамоса.
5. Остальное -- «Прочее».
"""

from __future__ import annotations

import functools
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DAM = os.path.join(ROOT, "firmware", "px5ns03d.dam")

# Порядок групп в дереве -- по тракту, а не по размеру.
ORDER = [
    "Зажигание",
    "Детонация",
    "Воздух и наполнение (ДМРВ)",
    "Состав смеси и полная нагрузка",
    "Впрыск и топливоподача",
    "Переходные режимы, плёнка",
    "Пуск и прогрев",
    "Моментная модель",
    "Фильтр момента, антирывок",
    "Холостой ход",
    "Отсечка и ограничение оборотов",
    "Лямбда-регулирование и адаптация",
    "Катализатор и задний зонд",
    "Адсорбер",
    "Кондиционер",
    "Охлаждение, вентилятор",
    "Пропуски воспламенения",
    "Датчики и прочие входы",
    "Диагностика и OBD",
    "Конфигурация, кодовые слова",
    "Рециркуляция ОГ (на моторе нет)",
    "Оси",
    "Найдено сканером",
    "Прочее",
]

IGN, KNOCK, AIR, MIX, INJ, TRANS, START, TORQ, FILT, IDLE, CUT, LAM, CAT, \
    TEV, AC, FAN, MISF, SENS, DIAG, CONF, EGR, AXES, SCAN, OTHER = ORDER

# Функция Bosch -> группа. Номера глав ФР в описаниях функций (19.40,
# 16.30 ...) по смыслу не группируют, поэтому таблица ручная.
FUNC = {
    # зажигание
    "ZWGRU": IGN, "ZUE": IGN, "ZUESZ": IGN, "ZWMIN": IGN, "ZWWL": IGN,
    "ZWSTT": IGN, "AZUE": IGN, "ETAZWG": IGN,
    # детонация
    "KRKE": KNOCK, "KRDY": KNOCK, "KRRA": KNOCK, "ZWKS": KNOCK,
    "GGKS": KNOCK, "DKRS": KNOCK, "DKRNT": KNOCK, "DKRTP": KNOCK,
    # воздух
    "BGSRM": AIR, "BGMSZS": AIR, "BGPU": AIR, "BGTEMPK": AIR, "BGRLP": AIR,
    "BGWDKM": AIR, "BGRML": AIR, "GGDKG": AIR, "DDKG": AIR, "DEGFE": AIR,
    "GGDSS": AIR, "DDSS": AIR, "SU": AIR, "BGTUMG": AIR, "PWMDKG": AIR,
    # смесь
    "LAMFAW": MIX, "LAMBTS": MIX, "LAMKO": MIX, "GK": MIX,
    # впрыск
    "AES": INJ, "RKTI": INJ, "ESGRU": INJ, "ESVW": INJ, "AEVAB": INJ,
    "ACIFI": INJ, "BGKV": INJ, "aekp": INJ, "DKVS": INJ,
    # переходные
    "ESUK": TRANS, "ESUKA": TRANS,
    # пуск
    "ESSTT": START, "ESNST": START, "ESWL": START, "STADAP": START,
    "BBSTT": START, "STMD": START, "MOTAUS": START, "ALE": START,
    # момент
    "MDBAS": TORQ, "MDIST": TORQ, "MDVER": TORQ, "MDVERAD": TORQ,
    "MDZW": TORQ, "MDMAX": TORQ, "MDMIN": TORQ, "MDRED": TORQ,
    "MDKOG": TORQ, "MDFAW": TORQ, "MDFUE": TORQ, "MDVERB": TORQ,
    "MDWAN": TORQ, "KHMD": TORQ, "GE": TORQ,
    "MDLWSD": FILT, "ARMD": FILT,
    # холостой
    "LLRNS": IDLE, "LLRRM": IDLE, "LLRBB": IDLE, "LLRAKL": IDLE,
    "LLDUB": IDLE, "FUELLS": IDLE, "ALLS": IDLE, "DLLR": IDLE,
    "DLLS": IDLE, "CLEANLLS": IDLE, "SHPOT": IDLE, "MDNSTAB": IDLE,
    "COSA": IDLE,
    # отсечка, обороты
    "BBSAWE": CUT, "MDSAWE": CUT, "ESWE": CUT, "NMAXMD": CUT,
    "VMAXMD": CUT, "BGTABST": CUT,
    # лямбда
    "LR": LAM, "LRA": LAM, "LREB": LAM, "LRAEB": LAM, "LRKA": LAM,
    "GGLSV": LAM, "DLSSA": LAM, "HLS": LAM, "DHLS": LAM, "DLSV": LAM,
    "DLSA": LAM,
    # катализатор
    "BBKHZ": CAT, "LAKH": CAT, "ATM": CAT, "DKAT": CAT, "DLSH": CAT,
    "DLSAHK": CAT, "LRHK": CAT, "GGLSH": CAT, "OVH": CAT,
    # адсорбер
    "TEB": TEV, "TEBEB": TEV, "BBTEGA": TEV, "ATEV": TEV, "DTEV": TEV,
    "BGTEV": TEV,
    # пропуски
    "DMDLU": MISF, "DMDLUA": MISF, "DMDDLU": MISF, "DMDTSB": MISF,
    "DMDFON": MISF, "DMDMIL": MISF, "DMDSTP": MISF, "DSWEB": MISF,
    "DMDLU_C": MISF,
    # прочее железо
    "KOS": AC, "MLS": FAN, "DTHM": FAN, "GGTFM": FAN,
    "GGTFA": SENS, "GGUB": SENS, "GGFST": SENS, "GGVFZG": SENS,
    "BBGANG": SENS, "GGDPG": SENS, "BGNG": SENS, "BGLBZ": SENS,
    "DDG": SENS, "DPH": SENS, "DAHC": SENS,
    # EGR
    "AGR": EGR, "AAGR": EGR, "ADAGRLS": EGR, "DAGRFC": EGR,
    "DAGRLS": EGR, "GGAGRV": EGR, "BGAGR": EGR,
    # диагностика, конфигурация
    "DMFB": DIAG, "DCLA": DIAG, "DIMC": DIAG, "DMIL": DIAG, "DWUC": DIAG,
    "DDCY": DIAG, "D2CTR": DIAG, "TC1MOD": DIAG, "TC5MOD": DIAG,
    "TC8MOD": DIAG, "T2DFA": DIAG, "KWP9A1": DIAG, "DFFTCNV": DIAG,
    "BGCVN": DIAG,
    "PROKON": CONF, "DEKON": CONF, "SSTB": CONF,
}

# Наши собственные записи и места, где функция дамоса не про этот мотор.
EXPLICIT = {
    # воздух: тракт ДМРВ разобран по коду (docs/26, docs/36)
    "HFM_LIN": AIR, "HFM_LIN_OFS": AIR, "KFKHFM": AIR, "KFPU": AIR,
    "KFPUSU": AIR, "KFPUNW": AIR, "KFPUSUNW": AIR, "PUKANS": AIR,
    "KUMSRL": AIR, "KLAF": AIR, "KFMSNWDK": AIR, "WDKUGDN": AIR,
    "KFRLW": AIR, "RLNOT": AIR, "RLVMXN": AIR, "RLVSMXN": AIR,
    "MSLG": AIR, "MSALLMN": AIR, "MSALLMX": AIR, "KIMSALL": AIR,
    "CWFKMSDKA": AIR, "KFMIRL": TORQ,
    # смесь и полная нагрузка
    "KLLAMFA_0": MIX, "KLLAMFA_1": MIX, "WDKVLN_0": MIX, "WDKVLN_1": MIX,
    "WDKSLN": IDLE, "TV_LAMFA": MIX, "KFLBTS": MIX, "KFFDLBTS": MIX,
    "CWLAMBTS": MIX, "KFLF": MIX, "LAM_RICH_MAX": MIX,
    "KL_LAM_OPEN": MIX, "KF_LAM_WARM": START, "ETALAM": TORQ,
    # впрыск
    "KRKTE": INJ, "KF_TI_NONLIN": INJ, "KFWEE": INJ, "KFWEEK": INJ,
    "WEEM": INJ,
    # плёнка
    "KFABAK": TRANS, "KFAVAK": TRANS, "KFBAKL": TRANS, "KFVAKL": TRANS,
    "KABAKL_L": TRANS, "KABAKL_S": TRANS, "KVAKL_L": TRANS,
    "DTH_ACC_ENR": TRANS, "DTH_DEC_LEAN": TRANS,
    # пуск
    "KST_COLD": START, "KST_COLD_MUL": START, "KST_COLD_NMUL": START,
    "KST_HOT": START, "KST_HOT_MUL": START, "KST_RESTART": START,
    # фильтр момента
    "KFDMDFA": FILT, "KFDMDFAS": FILT, "KFDMDFAU": FILT,
    "KFZMDFA": FILT, "KFZMDFAS": FILT, "KFZMDFAU": FILT,
    # обороты
    "NMAX": CUT, "NMAXDV": CUT, "DNMAXH": CUT, "TNMAXDV": CUT,
    "NMXDKPU": CUT,
    # лямбда и адаптация
    "TMOT_ADAPT": LAM, "CURVE_19515": LAM, "LS_CONFIG": CONF,
    "TMRA1": LAM, "TMRA2": LAM, "RLLRTMO": LAM, "RLLRUN": LAM,
    "TARAU": LAM, "TLRHS": LAM, "TASHS": LAM,
    # вентилятор
    "TMOT_FAN1": FAN, "TMOT_FAN2": FAN, "TMOT_FAN3": FAN, "TMOT_FAN4": FAN,
    "TMOT_FAN5": FAN, "TMOT_FAN_HYS": FAN, "TAN_FAN": FAN,
    # пропуски
    "NMIN_MISF": MISF, "NMAX_MISF": MISF,
    # холостой: в дамосе NLLM числится и в диагностике пропусков
    "NLLM": IDLE, "NFSM": IDLE,
    # кондиционер (docs/31)
    "WDKKOAN_0": AC, "WDKKOAN_1": AC, "WDKKOEN": AC, "LIMTKOA": AC,
    "TKOAMNN": AC, "TKOAMXN": AC, "TKOEMNN": AC, "TMKOAO": AC,
    "TMKOAU": AC, "TNACMX": AC, "TVKOA": AC, "TVKOE": AC, "VKOAO": AC,
    "VKOAU": AC,
    # диагностика
    "DTC_CODES": DIAG, "DIAG_FLAGS": DIAG,
    "CURVE_11A2E": OTHER, "CURVE_19509": MIX,
}

# Раздел профиля -> группа, для записей профиля, не попавших выше.
SECTION = {
    "torque_model": TORQ, "knock_control": KNOCK, "cyclic_charge": AIR,
    "hfm_path": AIR, "throttle_air_model": AIR, "saint_venant": AIR,
    "max_charge_at_wot": AIR, "fuel_path": INJ, "fuel_trim_curves": INJ,
    "transient_fuel": TRANS, "full_load_lambda": MIX,
    "full_load_enrichment": MIX, "alpha_map_KFLF": MIX,
    "lambda_control_thresholds": LAM, "mixture_adaptation": LAM,
    "second_lambda": CAT, "lambda_cat_diag_block": CAT,
    "canister_purge": TEV, "cpv_diagnostics_block": TEV, "cpv_curves": TEV,
    "ac_compressor": AC, "overrun_fuel_cut": CUT, "dtc_table": DIAG,
    "flag_tables": DIAG, "axes_resolved_from_code": AXES,
}

AXIS = re.compile(r"^S[A-Z0-9]{2}\d{2}[A-Z_]{2}[US][BW]$")


@functools.lru_cache(maxsize=1)
def damos_functions(path: str = DAM) -> dict:
    """{переменная: [функция, ...]} из записей /FKT дамоса."""
    out: dict = {}
    try:
        lines = open(path, "rb").read().decode("latin1").splitlines()
    except OSError:
        return out
    i = 0
    while i < len(lines):
        m = re.match(r"^/FKT, (\S+),", lines[i])
        if not m:
            i += 1
            continue
        fn = m.group(1)
        i += 1
        first = True
        while i < len(lines) and lines[i].strip().isdigit():
            n = int(lines[i])
            if first:                      # первый список -- калибровки
                for name in lines[i + 1:i + 1 + n]:
                    out.setdefault(name.strip(), []).append(fn)
                first = False
            i += 1 + n
    return out


def _base(name: str) -> str:
    """Имя без суффикса адреса у двойника (ETALAM_10515 -> ETALAM)."""
    if re.match(r"^(MAP|CURVE|GRID)_", name):
        return name
    return re.sub(r"_[0-9A-F]{5}$", "", name)


def category(name: str, section: str = "") -> str:
    b = _base(name)
    if b in EXPLICIT:
        return EXPLICIT[b]
    if AXIS.match(b):
        return AXES
    funcs = damos_functions()
    fl = funcs.get(b) or funcs.get(b + "_A") or []
    if fl:
        cats = [FUNC.get(f) or (DIAG if f.startswith("DFPM") else None) for f in fl]
        good = [c for c in cats if c and c not in (DIAG, CONF)]
        if good:
            return good[0]
        if any(cats):
            return next(c for c in cats if c)
    if re.match(r"^(MAP|CURVE|GRID)_\d", b):
        return SECTION.get(section) or SCAN
    if section in SECTION:
        return SECTION[section]
    if b.startswith("CW"):
        return CONF
    return OTHER
