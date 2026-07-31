"""
日程の自動割り当て

回答（誰がどの枠に○を付けたか）をもとに、次の条件で枠を割り当てる。

- 1つの時間枠には1人だけ（1対1）
- 人ごとに必要なコマ数を満たす
- 使う日数をできるだけ少なくする
- 同じ日の中ではできるだけ連続した時間になるようにする

割り当てはKuhnのアルゴリズム（二部マッチング）で行う。
必要コマ数が複数の人は、その数だけ「枠を1つ欲しい人」に分身させて扱う。
"""

from itertools import combinations


def split_slot(label: str):
    """'8/3(月) 10:00' → ('8/3(月)', '10:00')"""
    parts = label.rsplit(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (label, "")


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


def _match(units, avail, allowed):
    """units（枠を1つ欲しい人の一覧）をallowedの枠に割り当てる。
    枠番号が小さい順に埋めることで、結果的に早い時間・連続した時間に寄る。
    戻り値: (枠番号 → unitの添字, 割り当てできた数)"""
    owner: dict[int, int] = {}

    def try_assign(ui, seen):
        user = units[ui]
        for s in sorted(avail.get(user, ())):
            if s not in allowed or s in seen:
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


def auto_assign(candidates: list[str], votes: list[dict], quotas: dict) -> dict:
    """割り当てを実行して結果を返す。

    quotas: {user_id: コマ数}。未指定の人は1コマとして扱う。
    """
    avail, names = build_availability(candidates, votes)

    # 希望を出していて、かつコマ数1以上の人だけを対象にする
    users = [u for u in avail if int(quotas.get(u, 1)) > 0 and avail[u]]
    units = []
    for u in users:
        units.extend([u] * int(quotas.get(u, 1)))
    total_needed = len(units)

    if not total_needed:
        return {"ok": False, "error": "割り当て対象がいません。先に回答を集めてください。"}

    # 枠番号 → 日付。candidatesは時系列順に並んでいる前提。
    day_of = {i: split_slot(c)[0] for i, c in enumerate(candidates)}
    days = []
    for i in range(len(candidates)):
        if day_of[i] not in days:
            days.append(day_of[i])

    all_slots = set(range(len(candidates)))
    best_owner, best_days = None, None

    # 使う日数が少ない順に、その日だけで全員を収められるか試す
    if len(days) <= 10:
        for k in range(1, len(days) + 1):
            for combo in combinations(days, k):
                allowed = {i for i in all_slots if day_of[i] in combo}
                owner, matched = _match(units, avail, allowed)
                if matched == total_needed:
                    best_owner, best_days = owner, list(combo)
                    break
            if best_owner is not None:
                break

    # 日数を絞れない（または日数が多すぎて総当たりしない）場合は全枠で最大割り当て
    if best_owner is None:
        best_owner, _ = _match(units, avail, all_slots)
        best_days = days

    # 結果を整形
    assigned_count: dict[str, int] = {}
    schedule = []
    for slot_index in sorted(best_owner.keys()):
        user = units[best_owner[slot_index]]
        assigned_count[user] = assigned_count.get(user, 0) + 1
        day, time = split_slot(candidates[slot_index])
        schedule.append({"day": day, "time": time, "label": candidates[slot_index], "name": names[user]})

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
    }


def format_result(result: dict) -> str:
    """割り当て結果を人が読める文字列にする"""
    if not result.get("ok"):
        return result.get("error", "割り当てに失敗しました。")

    lines = ["=== 割り当て結果 ===", ""]
    lines.append(f"割り当て: {result['assigned']}/{result['needed']}コマ　使用日数: {len(result['used_days'])}日")
    lines.append("")

    current_day = None
    for s in result["schedule"]:
        if s["day"] != current_day:
            current_day = s["day"]
            lines.append(f"■ {current_day}")
        lines.append(f"  {s['time']}  {s['name']}")

    if result["shortfall"]:
        lines.append("")
        lines.append("=== 割り当てできなかった分 ===")
        lines.append("")
        for s in result["shortfall"]:
            lines.append(f"  {s['name']}　{s['got']}/{s['need']}コマ（希望した枠が他の人で埋まりました）")

    return "\n".join(lines)
