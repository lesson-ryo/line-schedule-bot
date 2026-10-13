"""
日程の自動割り当て

回答（誰がどの枠に○を付けたか）をもとに、次の条件で枠を割り当てる。

- 1つの時間帯には1組だけ
- 個人レッスンは1時間、グループレッスンは連続した2時間
- 2時間では全員が収まらない場合、必要な分だけグループを1.5時間に短縮して再試行する
- 開始時刻は30分単位（10:00 だけでなく 10:30 開始も可）
- グループは「参加できる人数が最も多い時間帯」に入れる（全員でなくてもよい）
- 使う日数をできるだけ少なくし、同じ日の中では連続した時間になるようにする
- 教室が複数ある場合、同じ日に別の教室へ移る際は教室どうしの移動時間を空ける
  （西中島↔新大阪 30分、それ以外 60分。TRAVEL_MINUTES で設定）
- 同じ日の教室移動はできるだけ少なく（1日あたり移動は最大1回）

回答フォームは1時間刻みのままで、30分単位になるのは割り当ての段階だけ。
「10:00 と 11:00 に○」＝「10:00〜12:00 は空いている」とみなして
10:30〜11:30 のような配置も可能にしている。
"""

from collections import Counter
import time

# 別の教室へ移るときに空ける時間（分）。教室名に含まれる言葉で判定する。
# 表にない組み合わせは DEFAULT_TRAVEL_MINUTES。
TRAVEL_MINUTES = {
    frozenset({"西中島", "新大阪"}): 30,
}
DEFAULT_TRAVEL_MINUTES = 60

# 「移動2回まで」を試すときの計算時間の上限（秒）。超えたら移動1回までの案だけで続ける。
TWO_MOVES_TIME_LIMIT = 6.0
_two_moves_deadline = 0.0

# この教室を選んだ人は、どの教室の時間帯にも入れられる
ANY_LOCATION = "どちらでもOK"

# 時間の最小単位（1時間 = 2コマ = 30分×2）
UNITS_PER_HOUR = 2

INDIVIDUAL_UNITS = 2  # 個人レッスン 1時間
GROUP_UNITS = 4       # グループレッスン 2時間
GROUP_UNITS_SHORT = 3  # 短縮時 1.5時間


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


def unit_to_time(unit: int) -> str:
    """30分単位の通し番号を '10:30' の形にする"""
    return f"{unit // UNITS_PER_HOUR}:{'30' if unit % UNITS_PER_HOUR else '00'}"


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


def build_entities(avail, names, quotas, locations, groups):
    """個人とグループをまとめて「枠を取り合う組」として整理する。"""
    entities: dict[str, dict] = {}

    for user_id, slots in avail.items():
        if not slots:
            continue
        group_name = (groups or {}).get(user_id, "").strip()
        quota = int((quotas or {}).get(user_id, 1))

        if group_name:
            e = entities.setdefault(
                group_name,
                {
                    "name": group_name,
                    "is_group": True,
                    "members": [],
                    "member_ids": [],
                    "avail": set(),
                    "attend": Counter(),
                    "locs": [],
                    "bookings": 1,
                },
            )
            e["members"].append(names.get(user_id, user_id))
            e["member_ids"].append(user_id)
            e["avail"] |= slots
            for s in slots:
                e["attend"][s] += 1
            loc = (locations or {}).get(user_id, "")
            if loc:
                e["locs"].append(loc)
        else:
            if quota <= 0:
                continue
            entities[user_id] = {
                "name": names.get(user_id, user_id),
                "is_group": False,
                "members": [names.get(user_id, user_id)],
                "member_ids": [user_id],
                "avail": set(slots),
                "attend": Counter({s: 1 for s in slots}),
                "locs": [l for l in [(locations or {}).get(user_id, "")] if l],
                "bookings": quota,
            }

    # グループの教室はメンバーの回答の多数決で決める。
    # 「どちらでもOK」しかいない場合はどちらにも入れられる扱いのまま。
    for e in entities.values():
        fixed = [l for l in e["locs"] if l != ANY_LOCATION]
        if fixed:
            e["location"] = Counter(fixed).most_common(1)[0][0]
        elif e["locs"]:
            e["location"] = ANY_LOCATION
        else:
            e["location"] = ""

    return entities


def _base_location(label: str) -> str:
    """'吹田教室（どちらでもOK）' → '吹田教室'（移動時間の判定用）"""
    return label.split("（")[0] if label else ""


def _resolve_location(location: str, seg_loc: str) -> str:
    """「どちらでもOK」の人が実際にどの教室の時間帯に入ったかを名前にする"""
    if location != ANY_LOCATION:
        return location
    return f"{seg_loc}（どちらでもOK）" if seg_loc else ANY_LOCATION


def _place_key(name: str) -> str:
    """教室名から地名のキーワードを取り出す（'西中島教室（どちらでもOK）' → '西中島'）"""
    base = _base_location(name)
    for pair in TRAVEL_MINUTES:
        for word in pair:
            if word in base:
                return word
    return base


def travel_minutes(a: str, b: str) -> int:
    """教室aから教室bへの移動に見ておく時間（分）。同じ教室なら0。"""
    ka, kb = _place_key(a), _place_key(b)
    if not ka or not kb or ka == kb:
        return 0
    return TRAVEL_MINUTES.get(frozenset({ka, kb}), DEFAULT_TRAVEL_MINUTES)


def travel_units(a: str, b: str) -> int:
    """移動時間を30分単位のコマ数にする（切り上げ）"""
    minutes = travel_minutes(a, b)
    per = 60 // UNITS_PER_HOUR
    return -(-minutes // per)


def _covered_hours(start_unit: int, length: int):
    """その配置が何時台にかかるかを返す（10:30〜11:30 なら 10時台と11時台）"""
    return sorted({(start_unit + k) // UNITS_PER_HOUR for k in range(length)})


def _starts_in(entity, length, hour_to_slot, free_units, allowed_units):
    """その組を、指定した時間（30分単位の集合）の中に入れられる開始位置の一覧"""
    starts = []
    if not allowed_units:
        return starts
    for u in range(min(allowed_units), max(allowed_units) - length + 2):
        block = range(u, u + length)
        if not all(b in free_units and b in allowed_units for b in block):
            continue
        hrs = _covered_hours(u, length)
        if not all(h in hour_to_slot and hour_to_slot[h] in entity["avail"] for h in hrs):
            continue
        starts.append(u)
    return starts


def _fits(entity_loc: str, segment_loc: str, loc_a: str) -> bool:
    """その組（希望教室）を、この教室の時間帯に入れてよいか"""
    loc = entity_loc or loc_a
    if loc == ANY_LOCATION or not segment_loc:
        return True
    return loc == segment_loc


def _fill_day(pending, entities, lengths, hour_to_slot, segments, loc_a):
    """1日分を埋める。入れられる場所が少ない組から順に、早い時間へ詰めていく。
    segments: [(教室名, 30分単位の集合), ...]（教室ごとの時間帯）
    戻り値: [(組ID, 開始unit, 長さ, 入った教室名), ...]"""
    free_units = set()
    for h in hour_to_slot:
        for k in range(UNITS_PER_HOUR):
            free_units.add(h * UNITS_PER_HOUR + k)

    placed = []
    remaining = list(pending)

    while True:
        options = []
        for idx, eid in enumerate(remaining):
            e = entities[eid]
            starts = []
            for seg_loc, seg_units in segments:
                if _fits(e["location"], seg_loc, loc_a):
                    starts.extend(
                        (u, seg_loc)
                        for u in _starts_in(e, lengths[eid], hour_to_slot, free_units, seg_units)
                    )
            if starts:
                starts.sort()
                options.append((len(starts), idx, eid, starts))
        if not options:
            break

        options.sort(key=lambda x: (x[0], not entities[x[2]]["is_group"], x[3][0][0]))
        _, idx, eid, starts = options[0]
        e = entities[eid]
        length = lengths[eid]

        if e["is_group"]:
            # 参加できる人数が最も多くなる開始位置を選ぶ（同点なら早い時間）
            def attendance(item):
                u = item[0]
                return min(e["attend"][hour_to_slot[h]] for h in _covered_hours(u, length))
            start, seg_loc = sorted(starts, key=lambda it: (-attendance(it), it[0]))[0]
        else:
            start, seg_loc = starts[0]

        placed.append((eid, start, length, seg_loc))
        for k in range(length):
            free_units.discard(start + k)
        remaining.pop(idx)

    return placed


def _day_plans(hours: list[int], loc_names: list[str], multi_loc: bool, loc_a: str, max_moves: int = 1):
    """その日の時間帯を教室にどう割り振るかの候補を列挙する。
    1日の教室は「1か所」か「前半と後半の2か所（移動1回）」が基本。
    max_moves=2 のときは「3つの時間帯（移動2回）」も候補にする。
    教室が変わるところは、その2教室の移動時間ぶん間を空ける。"""
    units = sorted({h * UNITS_PER_HOUR + k for h in hours for k in range(UNITS_PER_HOUR)})
    if not multi_loc:
        return [[(loc_a, set(units))]]
    plans = [[(loc, set(units))] for loc in loc_names]
    if not units:
        return plans
    for first in loc_names:
        for second in loc_names:
            if first == second:
                continue
            gap = travel_units(first, second)
            for x in range(units[0] + 1, units[-1] + 1):
                early = {u for u in units if u < x}
                late = {u for u in units if u >= x + gap}
                if early and late:
                    plans.append([(first, early), (second, late)])
    if max_moves >= 2 and time.monotonic() < _two_moves_deadline:
        # 3つの時間帯に分けるときは、区切りを毎正時にして候補数を抑える
        hour_units = [u for u in units if u % UNITS_PER_HOUR == 0]
        for first in loc_names:
            for second in loc_names:
                if first == second:
                    continue
                for third in loc_names:
                    if third == second:
                        continue
                    g1 = travel_units(first, second)
                    g2 = travel_units(second, third)
                    for x in hour_units:
                        early = {u for u in units if u < x}
                        if not early:
                            continue
                        for y in hour_units:
                            if y <= x + g1:
                                continue
                            mid = {u for u in units if x + g1 <= u < y}
                            late = {u for u in units if u >= y + g2}
                            if mid and late:
                                plans.append([(first, early), (second, mid), (third, late)])
    return plans


def _run(entities, lengths, candidates, days, slots_by_day, multi_loc, loc_a, loc_names, max_moves=1):
    """指定した長さで全日程の割り当てを試す。
    戻り値: (日付 → [(組ID, 開始unit, 長さ, 教室名)], 残った予約数)"""
    pending = []
    for eid, e in entities.items():
        pending.extend([eid] * e["bookings"])

    result: dict[str, list] = {}

    for day in days:
        if not pending:
            break
        hour_to_slot = slots_by_day[day]
        hours = sorted(hour_to_slot.keys())
        plans = _day_plans(hours, loc_names, multi_loc, loc_a, max_moves)

        best = None
        seen = set()
        for segments in plans:
            placed = _fill_day(pending, entities, lengths, hour_to_slot, segments, loc_a)
            if not placed:
                continue
            key = tuple(sorted(placed))
            if key in seen:
                continue
            seen.add(key)
            used = [u for _, u, _, _ in placed]
            seq = [loc for _, _, _, loc in sorted(placed, key=lambda p: p[1])]
            moves = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
            attend = sum(
                entities[eid]["attend"][hour_to_slot[h]]
                for eid, u, ln, _ in placed
                for h in _covered_hours(u, ln)
            )
            # 多く入る ＞ 移動が少ない ＞ 参加人数が多い ＞ 短い時間にまとまる ＞ 早い
            score = (-len(placed), moves, -attend, max(used) - min(used), min(used))
            if best is None or score < best[0]:
                best = (score, placed)

        if best is None:
            continue

        result[day] = best[1]
        for eid, _, _, _ in best[1]:
            pending.remove(eid)

    return result, len(pending)


def auto_assign(
    candidates: list[str],
    votes: list[dict],
    quotas: dict,
    locations: dict | None = None,
    groups: dict | None = None,
) -> dict:
    """割り当てを実行して結果を返す。
    まず「1日の教室移動は1回まで」で組み、入りきらない人がいるときだけ
    「移動2回まで」も試して、多く入るほうを採用する。"""
    result = _auto_assign(candidates, votes, quotas, locations, groups, max_moves=1)
    if result.get("ok") and result["shortfall"] and len(result.get("locations_used", [])) >= 2:
        global _two_moves_deadline
        _two_moves_deadline = time.monotonic() + TWO_MOVES_TIME_LIMIT
        more = _auto_assign(candidates, votes, quotas, locations, groups, max_moves=2)
        if more.get("ok") and more["assigned"] > result["assigned"]:
            return more
    return result


def _auto_assign(candidates, votes, quotas, locations, groups, max_moves):
    avail, names = build_availability(candidates, votes)
    entities = build_entities(avail, names, quotas, locations, groups)

    if not entities:
        return {"ok": False, "error": "割り当て対象がいません。先に回答を集めてください。"}

    total_needed = sum(e["bookings"] for e in entities.values())

    day_of = {i: split_slot(c)[0] for i, c in enumerate(candidates)}
    days = []
    for i in range(len(candidates)):
        if day_of[i] not in days:
            days.append(day_of[i])

    slots_by_day: dict[str, dict] = {}
    for i in range(len(candidates)):
        slots_by_day.setdefault(day_of[i], {})[slot_hour(candidates[i])] = i

    loc_names = []
    for e in entities.values():
        loc = e["location"]
        if loc and loc != ANY_LOCATION and loc not in loc_names:
            loc_names.append(loc)
    loc_a = loc_names[0] if loc_names else ""
    multi_loc = len(loc_names) >= 2

    group_ids = [eid for eid, e in entities.items() if e["is_group"]]

    # まずは全グループ2時間で試す
    lengths = {
        eid: (GROUP_UNITS if e["is_group"] else INDIVIDUAL_UNITS)
        for eid, e in entities.items()
    }
    placement, left = _run(entities, lengths, candidates, days, slots_by_day, multi_loc, loc_a, loc_names, max_moves)
    shortened: set = set()

    def try_with(short_set):
        trial = {
            eid: (
                (GROUP_UNITS_SHORT if eid in short_set else GROUP_UNITS)
                if e["is_group"]
                else INDIVIDUAL_UNITS
            )
            for eid, e in entities.items()
        }
        return _run(entities, trial, candidates, days, slots_by_day, multi_loc, loc_a, loc_names, max_moves)

    # 収まらなければ、グループを順に1.5時間へ短縮していく
    # （1組だけでは足りず、複数まとめて短縮して初めて収まる場合があるため累積で試す）
    if left > 0:
        acc: set = set()
        for g in group_ids:
            acc.add(g)
            trial_placement, trial_left = try_with(acc)
            if trial_left < left:
                placement, left, shortened = trial_placement, trial_left, set(acc)
            if left == 0:
                break

        # 短縮しなくても収まるグループは2時間に戻す（短縮を必要最小限にする）
        for g in list(shortened):
            candidate_set = shortened - {g}
            trial_placement, trial_left = try_with(candidate_set)
            if trial_left <= left:
                placement, left, shortened = trial_placement, trial_left, candidate_set

    # 結果を整形
    schedule = []
    assigned_count: Counter = Counter()
    for day in days:
        for eid, start, length, seg_loc in sorted(placement.get(day, []), key=lambda x: x[1]):
            e = entities[eid]
            assigned_count[eid] += 1
            hour_to_slot = slots_by_day[day]
            hrs = _covered_hours(start, length)

            attend = min(e["attend"][hour_to_slot[h]] for h in hrs) if e["is_group"] else 1
            absent = []
            if e["is_group"]:
                for user_id in e["member_ids"]:
                    if not all(hour_to_slot[h] in avail.get(user_id, set()) for h in hrs):
                        absent.append(names.get(user_id, user_id))

            schedule.append(
                {
                    "day": day,
                    "time": unit_to_time(start),
                    "end": unit_to_time(start + length),
                    "name": e["name"],
                    "location": _resolve_location(e["location"], seg_loc),
                    "is_group": e["is_group"],
                    "shortened": eid in shortened,
                    "attend": attend,
                    "total": len(e["members"]),
                    "absent": absent,
                    "member_ids": list(e["member_ids"]),
                    "start_unit": start,
                    "length": length,
                }
            )

    shortfall = []
    for eid, e in entities.items():
        got = assigned_count.get(eid, 0)
        if got < e["bookings"]:
            shortfall.append({"name": e["name"], "need": e["bookings"], "got": got})

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
        "shortened": sorted(entities[g]["name"] for g in shortened),
    }


def check_conflicts(result: dict) -> list[str]:
    """時間の重複・教室間の間隔不足がないか検算する。"""
    problems = []
    by_day: dict[str, list] = {}
    for s in result["schedule"]:
        by_day.setdefault(s["day"], []).append(s)

    for day, items in by_day.items():
        items = sorted(items, key=lambda x: x["start_unit"])
        for i in range(len(items)):
            a = items[i]
            a_end = a["start_unit"] + a["length"]
            for j in range(i + 1, len(items)):
                b = items[j]
                if b["start_unit"] < a_end:
                    problems.append(
                        f"{day} {a['time']}-{a['end']}({a['name']}) と "
                        f"{b['time']}-{b['end']}({b['name']}) が重複しています"
                    )
                    continue
                la, lb = _base_location(a["location"]), _base_location(b["location"])
                if not la or not lb or la == lb:
                    continue
                gap_units = b["start_unit"] - a_end
                if gap_units < travel_units(la, lb):
                    problems.append(
                        f"{day} {a['end']}({a['location']}) から "
                        f"{b['time']}({b['location']}) の移動時間が足りません"
                    )
    return problems


def format_result(result: dict) -> str:
    """割り当て結果を人が読める文字列にする"""
    if not result.get("ok"):
        return result.get("error", "割り当てに失敗しました。")

    lines = ["=== 割り当て結果 ===", ""]
    lines.append(
        f"割り当て: {result['assigned']}/{result['needed']}組　使用日数: {len(result['used_days'])}日"
    )
    if len(result.get("locations_used", [])) >= 2:
        lines.append("※ 教室が変わる前後は移動時間（西中島↔新大阪 30分、吹田との間 60分）を空けています")
    if result.get("shortened"):
        lines.append(f"※ 全員を収めるため次のグループを1.5時間に短縮: {', '.join(result['shortened'])}")
    lines.append("")

    current_day = None
    for s in result["schedule"]:
        if s["day"] != current_day:
            current_day = s["day"]
            lines.append(f"■ {current_day}")
        loc = f"　[{s['location']}]" if s["location"] else ""
        mark = "　※1.5時間に短縮" if s["shortened"] else ""
        if s["is_group"]:
            lines.append(
                f"  {s['time']}-{s['end']}  {s['name']}（{s['attend']}/{s['total']}人）{loc}{mark}"
            )
            if s["absent"]:
                lines.append(f"              参加不可: {', '.join(s['absent'])}")
        else:
            lines.append(f"  {s['time']}-{s['end']}  {s['name']}{loc}")

    if result["shortfall"]:
        lines.append("")
        lines.append("=== 割り当てできなかった分 ===")
        lines.append("")
        for s in result["shortfall"]:
            lines.append(f"  {s['name']}　{s['got']}/{s['need']}件（希望した枠が埋まりました）")

    return "\n".join(lines)
