"""
日程の自動割り当て

回答（誰がどの枠に○を付けたか）をもとに、次の条件で枠を割り当てる。

- 1つの時間枠には1組だけ
- 個人レッスンは1枠（1時間）、グループレッスンは連続した2枠（2時間）
- グループは「参加できる人数が最も多い時間帯」に入れる（全員でなくてもよい）
- 使う日数をできるだけ少なくし、同じ日の中では連続した時間になるようにする
- 教室が複数ある場合、同じ日に別の教室へ移る際は1時間以上空ける（移動時間）

日ごとに「どの時間帯をどの教室に使うか」を決めながら、
制約の厳しい組（＝入れられる場所が少ない組）から順に早い時間へ詰めていく。
"""

from collections import Counter

# 別の教室へ移るときに空ける時間（時間単位）。
# 2なら「10時に終わった次は12時から」という意味になり、間に1時間の空きができる。
TRAVEL_GAP = 2

# グループレッスンが使う枠数（1枠=1時間）
GROUP_SLOTS = 2


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


def build_entities(avail, names, quotas, locations, groups):
    """個人とグループをまとめて「枠を取り合う組」として整理する。

    groups: {user_id: グループ名}。未設定・空文字なら個人レッスン扱い。
    """
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
                    "length": GROUP_SLOTS,  # 連続2枠
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
                "length": 1,
                "bookings": quota,
            }

    # グループの教室はメンバーの回答の多数決で決める
    for e in entities.values():
        e["location"] = Counter(e["locs"]).most_common(1)[0][0] if e["locs"] else ""

    return entities


def _starts_for(entity, day_slots_by_hour, free_hours, allowed_hours):
    """その組をこの日に入れられる開始時刻の一覧を返す。
    グループは連続した枠が必要なので、続きの時刻も空いているか確認する。"""
    starts = []
    for h in sorted(allowed_hours):
        block = [h + k for k in range(entity["length"])]
        if not all(
            b in free_hours and b in allowed_hours and day_slots_by_hour.get(b) is not None
            for b in block
        ):
            continue
        if not all(day_slots_by_hour[b] in entity["avail"] for b in block):
            continue
        starts.append(h)
    return starts


def _fill_day(pending, entities, day_slots_by_hour, hours_a, hours_b, loc_a):
    """1日分を埋める。制約の厳しい組から順に、早い時間へ詰めていく。
    戻り値: {組ID: [枠番号...]}"""
    free = set(day_slots_by_hour.keys())
    placed: dict[str, list] = {}
    remaining = list(pending)

    while True:
        options = []
        for idx, eid in enumerate(remaining):
            e = entities[eid]
            allowed = hours_a if (e["location"] or loc_a) == loc_a else hours_b
            starts = _starts_for(e, day_slots_by_hour, free, allowed)
            if starts:
                options.append((len(starts), idx, eid, starts))
        if not options:
            break

        # 入れられる場所が少ない組を優先。同数ならグループを先に。
        options.sort(key=lambda x: (x[0], not entities[x[2]]["is_group"], x[3][0]))
        _, idx, eid, starts = options[0]
        e = entities[eid]

        if e["is_group"]:
            # 参加人数が最も多くなる開始時刻を選ぶ
            def attendance(h):
                return sum(e["attend"][day_slots_by_hour[h + k]] for k in range(e["length"]))
            start = sorted(starts, key=lambda h: (-attendance(h), h))[0]
        else:
            start = starts[0]

        block = [day_slots_by_hour[start + k] for k in range(e["length"])]
        placed.setdefault(eid, []).extend(block)
        for k in range(e["length"]):
            free.discard(start + k)
        remaining.pop(idx)

    return placed


def _day_plans(hours: list[int]):
    """その日の時間帯を2つの教室にどう割り振るかの候補を列挙する。
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


def auto_assign(
    candidates: list[str],
    votes: list[dict],
    quotas: dict,
    locations: dict | None = None,
    groups: dict | None = None,
) -> dict:
    """割り当てを実行して結果を返す。

    quotas: {user_id: コマ数}。個人のみ有効（グループは常に連続2枠）。
    locations: {user_id: 教室名}。2種類以上あるときは教室間に移動時間を確保する。
    groups: {user_id: グループ名}。同じ名前の人は1組として扱う。
    """
    avail, names = build_availability(candidates, votes)
    entities = build_entities(avail, names, quotas, locations, groups)

    if not entities:
        return {"ok": False, "error": "割り当て対象がいません。先に回答を集めてください。"}

    # 予約1件を1エントリとして展開（個人がコマ数2なら2件）
    pending = []
    for eid, e in entities.items():
        pending.extend([eid] * e["bookings"])
    total_needed = len(pending)

    day_of = {i: split_slot(c)[0] for i, c in enumerate(candidates)}
    days = []
    for i in range(len(candidates)):
        if day_of[i] not in days:
            days.append(day_of[i])

    loc_names = []
    for e in entities.values():
        if e["location"] and e["location"] not in loc_names:
            loc_names.append(e["location"])
    loc_a = loc_names[0] if loc_names else ""
    multi_loc = len(loc_names) >= 2

    slots_by_day: dict[str, dict] = {}
    for i in range(len(candidates)):
        slots_by_day.setdefault(day_of[i], {})[slot_hour(candidates[i])] = i

    assignments: dict[int, str] = {}  # 枠番号 → 組ID

    for day in days:
        if not pending:
            break
        day_slots_by_hour = slots_by_day[day]
        hours = sorted(day_slots_by_hour.keys())
        plans = _day_plans(hours) if multi_loc else [(set(hours), set(hours))]

        best = None
        for hours_a, hours_b in plans:
            placed = _fill_day(pending, entities, day_slots_by_hour, hours_a, hours_b, loc_a)
            count = sum(len(v) // entities[k]["length"] for k, v in placed.items())
            if not count:
                continue
            used = sorted(slot_hour(candidates[s]) for v in placed.values() for s in v)
            attend = sum(entities[k]["attend"][s] for k, v in placed.items() for s in v)
            score = (-count, -attend, used[-1] - used[0], used[0])
            if best is None or score < best[0]:
                best = (score, placed)

        if best is None:
            continue

        for eid, slots in best[1].items():
            length = entities[eid]["length"]
            for n in range(len(slots) // length):
                for s in slots[n * length:(n + 1) * length]:
                    assignments[s] = eid
                pending.remove(eid)

    # 結果を整形（同じ組の連続枠は1件にまとめる）
    schedule = []
    done = set()
    for slot_index in sorted(assignments.keys()):
        if slot_index in done:
            continue
        eid = assignments[slot_index]
        e = entities[eid]
        block = [slot_index]
        for k in range(1, e["length"]):
            nxt = slot_index + k
            if assignments.get(nxt) == eid:
                block.append(nxt)
        done.update(block)

        day, start_time = split_slot(candidates[block[0]])
        end_hour = slot_hour(candidates[block[-1]]) + 1
        attend = min(e["attend"][s] for s in block) if e["is_group"] else 1

        absent = []
        if e["is_group"]:
            for user_id in e["member_ids"]:
                if not all(s in avail.get(user_id, set()) for s in block):
                    absent.append(names.get(user_id, user_id))

        schedule.append(
            {
                "day": day,
                "time": start_time,
                "end": f"{end_hour}:00",
                "label": candidates[block[0]],
                "name": e["name"],
                "location": e["location"],
                "is_group": e["is_group"],
                "attend": attend,
                "total": len(e["members"]),
                "absent": absent,
                "slots": block,
            }
        )

    assigned_count = Counter(assignments[s] for s in assignments)
    shortfall = []
    for eid, e in entities.items():
        got = assigned_count.get(eid, 0) // e["length"]
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
    }


def check_conflicts(result: dict) -> list[str]:
    """枠の重複・教室間の間隔不足がないか検算する。"""
    problems = []
    seen = {}
    for s in result["schedule"]:
        for slot in s["slots"]:
            if slot in seen:
                problems.append(f"枠が重複: {s['label']}（{seen[slot]} と {s['name']}）")
            seen[slot] = s["name"]

    by_day: dict[str, list] = {}
    for s in result["schedule"]:
        by_day.setdefault(s["day"], []).append(s)

    for day, items in by_day.items():
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if not a["location"] or not b["location"] or a["location"] == b["location"]:
                    continue
                try:
                    a_start = int(a["time"].split(":")[0])
                    a_end = int(a["end"].split(":")[0])
                    b_start = int(b["time"].split(":")[0])
                    b_end = int(b["end"].split(":")[0])
                except (ValueError, IndexError):
                    continue
                # 先に終わる方から次の教室の開始までの間隔を見る
                gap = b_start - a_end if b_start >= a_end else a_start - b_end
                if gap < TRAVEL_GAP - 1:
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
        f"割り当て: {result['assigned']}/{result['needed']}組　使用日数: {len(result['used_days'])}日"
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
        if s["is_group"]:
            lines.append(
                f"  {s['time']}-{s['end']}  {s['name']}（{s['attend']}/{s['total']}人）{loc}"
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
