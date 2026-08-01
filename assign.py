"""
日程の自動割り当て

回答（誰がどの枠に○を付けたか）をもとに、次の条件で枠を割り当てる。

- 1つの時間帯には1組だけ
- 個人レッスンは1時間、グループレッスンは連続した2時間
- 2時間では全員が収まらない場合、必要な分だけグループを1.5時間に短縮して再試行する
- 開始時刻は30分単位（10:00 だけでなく 10:30 開始も可）
- グループは「参加できる人数が最も多い時間帯」に入れる（全員でなくてもよい）
- 使う日数をできるだけ少なくし、同じ日の中では連続した時間になるようにする
- 教室が複数ある場合、同じ日に別の教室へ移る際は1時間以上空ける（移動時間）

回答フォームは1時間刻みのままで、30分単位になるのは割り当ての段階だけ。
「10:00 と 11:00 に○」＝「10:00〜12:00 は空いている」とみなして
10:30〜11:30 のような配置も可能にしている。
"""

from collections import Counter

# 別の教室へ移るときに空ける時間（時間単位）
TRAVEL_GAP = 2

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


def _resolve_location(location: str, side: str | None, loc_names: list[str]) -> str:
    """「どちらでもOK」の人が実際にどの教室の時間帯に入ったかを名前にする"""
    if location != ANY_LOCATION:
        return location
    if side == "a" and len(loc_names) >= 1:
        return f"{loc_names[0]}（どちらでもOK）"
    if side == "b" and len(loc_names) >= 2:
        return f"{loc_names[1]}（どちらでもOK）"
    return ANY_LOCATION


def _covered_hours(start_unit: int, length: int):
    """その配置が何時台にかかるかを返す（10:30〜11:30 なら 10時台と11時台）"""
    return sorted({(start_unit + k) // UNITS_PER_HOUR for k in range(length)})


def _starts_for(entity, length, hour_to_slot, free_units, allowed_hours):
    """その組をこの日に入れられる開始位置（30分単位）の一覧"""
    starts = []
    hours = sorted(hour_to_slot.keys())
    if not hours:
        return starts

    first_unit = hours[0] * UNITS_PER_HOUR
    last_unit = (hours[-1] + 1) * UNITS_PER_HOUR - 1

    for u in range(first_unit, last_unit - length + 2):
        block = list(range(u, u + length))
        if not all(b in free_units for b in block):
            continue
        hrs = _covered_hours(u, length)
        if not all(h in hour_to_slot and h in allowed_hours for h in hrs):
            continue
        if not all(hour_to_slot[h] in entity["avail"] for h in hrs):
            continue
        starts.append(u)
    return starts


def _fill_day(pending, entities, lengths, hour_to_slot, hours_a, hours_b, loc_a):
    """1日分を埋める。入れられる場所が少ない組から順に、早い時間へ詰めていく。
    戻り値: [(組ID, 開始unit, 長さ), ...]"""
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
            loc = e["location"] or loc_a
            if loc == ANY_LOCATION:
                allowed = hours_a | hours_b  # どちらの教室の時間帯でもよい
            else:
                allowed = hours_a if loc == loc_a else hours_b
            starts = _starts_for(e, lengths[eid], hour_to_slot, free_units, allowed)
            if starts:
                options.append((len(starts), idx, eid, starts))
        if not options:
            break

        options.sort(key=lambda x: (x[0], not entities[x[2]]["is_group"], x[3][0]))
        _, idx, eid, starts = options[0]
        e = entities[eid]
        length = lengths[eid]

        if e["is_group"]:
            # 参加できる人数が最も多くなる開始位置を選ぶ（同点なら早い時間）
            def attendance(u):
                return min(e["attend"][hour_to_slot[h]] for h in _covered_hours(u, length))
            start = sorted(starts, key=lambda u: (-attendance(u), u))[0]
        else:
            start = starts[0]

        # 「どちらでもOK」の場合、実際にどちらの教室の時間帯に入ったかを記録する
        side = None
        if (e["location"] or loc_a) == ANY_LOCATION:
            hrs = _covered_hours(start, length)
            side = "a" if all(h in hours_a for h in hrs) else "b"

        placed.append((eid, start, length, side))
        for k in range(length):
            free_units.discard(start + k)
        remaining.pop(idx)

    return placed


def _day_plans(hours: list[int]):
    """その日の時間帯を2つの教室にどう割り振るかの候補を列挙する。"""
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


def _run(entities, lengths, candidates, days, slots_by_day, multi_loc, loc_a):
    """指定した長さで全日程の割り当てを試す。
    戻り値: (日付 → [(組ID, 開始unit, 長さ)], 残った予約数)"""
    pending = []
    for eid, e in entities.items():
        pending.extend([eid] * e["bookings"])

    result: dict[str, list] = {}

    for day in days:
        if not pending:
            break
        hour_to_slot = slots_by_day[day]
        hours = sorted(hour_to_slot.keys())
        plans = _day_plans(hours) if multi_loc else [(set(hours), set(hours))]

        best = None
        for hours_a, hours_b in plans:
            placed = _fill_day(pending, entities, lengths, hour_to_slot, hours_a, hours_b, loc_a)
            if not placed:
                continue
            used = [u for _, u, _, _ in placed]
            attend = sum(
                entities[eid]["attend"][hour_to_slot[h]]
                for eid, u, ln, _ in placed
                for h in _covered_hours(u, ln)
            )
            score = (-len(placed), -attend, max(used) - min(used), min(used))
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
    """割り当てを実行して結果を返す。"""
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
    placement, left = _run(entities, lengths, candidates, days, slots_by_day, multi_loc, loc_a)
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
        return _run(entities, trial, candidates, days, slots_by_day, multi_loc, loc_a)

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
        for eid, start, length, side in sorted(placement.get(day, []), key=lambda x: x[1]):
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
                    "location": _resolve_location(e["location"], side, loc_names),
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


def free_slots_next_to_bookings(candidates: list[str], schedule: list[dict], location: str = "") -> list[str]:
    """確定済みの枠のすぐ前後にある空き枠だけを返す。
    予定が飛び飛びにならないよう、離れた時間帯は候補にしない。
    location を指定すると、その教室の枠に隣接するものだけに絞る（移動時間の都合）。"""
    occupied: dict[tuple, str] = {}
    for s in schedule:
        for h in _covered_hours(s["start_unit"], s["length"]):
            occupied[(s["day"], h)] = _base_location(s.get("location", ""))

    want = _base_location(location)
    result = []
    for c in candidates:
        day, _ = split_slot(c)
        hour = slot_hour(c)
        if (day, hour) in occupied:
            continue
        for nb in ((day, hour - 1), (day, hour + 1)):
            if nb not in occupied:
                continue
            here = occupied[nb]
            # 教室が指定されている場合、同じ教室の枠に隣接するものだけ
            if want and here and want != here and location != ANY_LOCATION:
                continue
            result.append(c)
            break
    return result


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
                if gap_units < (TRAVEL_GAP - 1) * UNITS_PER_HOUR:
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
        lines.append(f"※ 教室が変わる前後は{TRAVEL_GAP - 1}時間以上空けています")
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
