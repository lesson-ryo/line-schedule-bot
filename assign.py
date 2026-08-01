"""
日程の自動割り当て

回答（誰がどの枠に○を付けたか）をもとに、次の条件で枠を割り当てる。

- 1つの時間枠には1人だけ（1対1）
- 人ごとに必要なコマ数を満たす
- 使う日数をできるだけ少なくする
- 同じ日の中ではできるだけ連続した時間になるようにする
- 教室が複数ある場合、同じ日に別の教室へ移る際は1時間以上空ける（移動時間）

割り当てはKuhnのアルゴリズム（二部マッチング）で行う。
必要コマ数が複数の人は、その数だけ「枠を1つ欲しい人」に分身させて扱う。
"""

from itertools import combinations

# 別の教室へ移るときに空ける時間（時間単位）。
# 2なら「10時の枠の次は12時から」という意味になり、間に1時間の空きができる。
TRAVEL_GAP = 2


def split_slot(label: str):
    """'8/3(月) 10:00' → ('8/3(月)', '10:00')"""
    parts = label.rsplit(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (label, "")


def slot_hour(label: str) -> int:
    """'8/3(月) 10:00' → 10。読めない場合は-1。"""
    _, time = split_slot(label)
    try:
        return int(time.split(":")[0])
    except (ValueError, IndexError):
        return -1


def build_availability(candidates: list[str], votes: list[dict]):
    """回答から「その人が○を付けた枠番号(0始まり)の集合」を作る"""
    avail: dict[str, set] = {}
    names: dict[str, str] = {}
    for v in votes:
        idx = v.get("candidate_index", 0)
        if 0 < idx <= len(candidates):
            avail.setdefault(v["user_id"], set()).add(idx - 1)
            names[v["user_id"]] = v.get("display_name", v["user_id"])
    return avail, names


def _match(units, avail, slot_ok):
    """units（枠を1つ欲しい人の一覧）を割り当てる。
    slot_ok(unit番号, 枠番号) がTrueの枠だけ使える。
    枠番号が小さい順に埋めるので、結果的に早い時間・連続した時間に寄る。
    戻り値: (枠番号 → unitの添字, 割り当てできた数)"""
    owner: dict[int, int] = {}

    def try_assign(ui, seen):
        for s in sorted(avail.get(units[ui], ())):
            if s in seen or not slot_ok(ui, s):
                continue
            seen.add(s)
            if s not in owner or try_assign(owner[s], seen):
                owner[s] = ui
                return True
        return False

    matched = 0
    for ui in range(len(units)):
        if try_assign(ui, set()):
            matched += 1
    return owner, matched


def _assign_single_location(units, avail, candidates, day_of, days):
    """教室が1つ以下の場合。使う日数が最小になる組み合わせを探す。"""
    all_slots = set(range(len(candidates)))
    total = len(units)

    if len(days) <= 10:
        for k in range(1, len(days) + 1):
            for combo in combinations(days, k):
                allowed = {i for i in all_slots if day_of[i] in combo}
                owner, matched = _match(units, avail, lambda ui, s: s in allowed)
                if matched == total:
                    return owner
    owner, _ = _match(units, avail, lambda ui, s: True)
    return owner


def _day_plans(hours: list[int]):
    """その日の時間帯を2つの教室にどう割り振るかの候補を列挙する。
    (教室Aが使える時刻の集合, 教室Bが使える時刻の集合) を返す。
    別教室の間はTRAVEL_GAP時間以上あける。"""
    plans = [(set(hours), set()), (set(), set(hours))]
    if not hours:
        return plans
    for x in range(min(hours), max(hours) + 1):
        early = {h for h in hours if h <= x}
        late = {h for h in hours if h >= x + TRAVEL_GAP}
        if early and late:
            plans.append((early, late))
            plans.append((late, early))
    return plans


def _assign_two_locations(units, unit_loc, avail, candidates, day_of, days, loc_names):
    """教室が2つ以上の場合。日ごとに「どの時間帯をどの教室に使うか」を決めながら、
    早い日・早い時間から詰めていく。"""
    loc_a, loc_b = loc_names[0], loc_names[1]
    slots_of_day = {}
    for i in range(len(candidates)):
        slots_of_day.setdefault(day_of[i], []).append(i)

    owner: dict[int, int] = {}
    remaining = list(range(len(units)))

    for day in days:
        if not remaining:
            break
        day_slots = slots_of_day[day]
        hours = sorted({slot_hour(candidates[i]) for i in day_slots})

        best = None
        for hours_a, hours_b in _day_plans(hours):
            def ok(ri, s, ha=hours_a, hb=hours_b):
                if s not in day_slots:
                    return False
                h = slot_hour(candidates[s])
                loc = unit_loc.get(units[remaining[ri]], loc_a)
                return h in (ha if loc == loc_a else hb)

            sub_units = [units[r] for r in remaining]
            sub_owner, matched = _match(sub_units, avail, ok)
            if not matched:
                continue
            used = sorted(slot_hour(candidates[s]) for s in sub_owner)
            score = (-matched, used[-1] - used[0], used[0])
            if best is None or score < best[0]:
                best = (score, sub_owner)

        if best is None:
            continue
        for s, ri in best[1].items():
            owner[s] = remaining[ri]
        taken = set(best[1].values())
        remaining = [r for i, r in enumerate(remaining) if i not in taken]

    return owner


def auto_assign(candidates: list[str], votes: list[dict], quotas: dict, locations: dict | None = None) -> dict:
    """割り当てを実行して結果を返す。

    quotas: {user_id: コマ数}。未指定の人は1コマとして扱う。
    locations: {user_id: 教室名}。2種類以上あるときは教室間に移動時間を確保する。
    """
    avail, names = build_availability(candidates, votes)
    locations = locations or {}

    users = [u for u in avail if int(quotas.get(u, 1)) > 0 and avail[u]]
    units = []
    for u in users:
        units.extend([u] * int(quotas.get(u, 1)))
    total_needed = len(units)

    if not total_needed:
        return {"ok": False, "error": "割り当て対象がいません。先に回答を集めてください。"}

    day_of = {i: split_slot(c)[0] for i, c in enumerate(candidates)}
    days = []
    for i in range(len(candidates)):
        if day_of[i] not in days:
            days.append(day_of[i])

    # 回答者が選んだ教室の種類
    loc_names = []
    for u in users:
        l = locations.get(u)
        if l and l not in loc_names:
            loc_names.append(l)

    if len(loc_names) >= 2:
        owner = _assign_two_locations(units, locations, avail, candidates, day_of, days, loc_names)
    else:
        owner = _assign_single_location(units, avail, candidates, day_of, days)

    assigned_count: dict[str, int] = {}
    schedule = []
    for slot_index in sorted(owner.keys()):
        user = units[owner[slot_index]]
        assigned_count[user] = assigned_count.get(user, 0) + 1
        day, time = split_slot(candidates[slot_index])
        schedule.append(
            {
                "day": day,
                "time": time,
                "label": candidates[slot_index],
                "name": names[user],
                "location": locations.get(user, ""),
            }
        )

    shortfall = []
    for u in users:
        need = int(quotas.get(u, 1))
        got = assigned_count.get(u, 0)
        if got < need:
            shortfall.append({"name": names[u], "need": need, "got": got})

    used_days = []
    for s in schedule:
        if s["day"] not in used_days:
            used_days.append(s["day"])

    return {
        "ok": True,
        "schedule": schedule,
        "shortfall": shortfall,
        "assigned": len(schedule),
        "needed": total_needed,
        "used_days": used_days,
        "locations_used": loc_names,
    }


def check_travel_gap(schedule: list[dict]) -> list[str]:
    """同じ日に別教室の枠が近すぎないかを検算する。問題があれば説明を返す。"""
    problems = []
    by_day: dict[str, list] = {}
    for s in schedule:
        by_day.setdefault(s["day"], []).append(s)

    for day, items in by_day.items():
        items = sorted(items, key=lambda x: x["time"])
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if not a["location"] or not b["location"] or a["location"] == b["location"]:
                    continue
                try:
                    gap = int(b["time"].split(":")[0]) - int(a["time"].split(":")[0])
                except (ValueError, IndexError):
                    continue
                if gap < TRAVEL_GAP:
                    problems.append(
                        f"{day} {a['time']}({a['location']}) と {b['time']}({b['location']}) の間隔が足りません"
                    )
    return problems


def format_result(result: dict) -> str:
    """割り当て結果を人が読める文字列にする"""
    if not result.get("ok"):
        return result.get("error", "割り当てに失敗しました。")

    lines = ["=== 割り当て結果 ===", ""]
    lines.append(
        f"割り当て: {result['assigned']}/{result['needed']}コマ　使用日数: {len(result['used_days'])}日"
    )
    if len(result.get("locations_used", [])) >= 2:
        lines.append(f"※ 教室が変わる前後は{TRAVEL_GAP - 1}時間以上空けています")
    lines.append("")

    current_day = None
    for s in result["schedule"]:
        if s["day"] != current_day:
            current_day = s["day"]
            lines.append(f"■ {current_day}")
        loc = f"　[{s['location']}]" if s["location"] else ""
        lines.append(f"  {s['time']}  {s['name']}{loc}")

    if result["shortfall"]:
        lines.append("")
        lines.append("=== 割り当てできなかった分 ===")
        lines.append("")
        for s in result["shortfall"]:
            lines.append(f"  {s['name']}　{s['got']}/{s['need']}コマ（希望した枠が埋まりました）")

    return "\n".join(lines)
