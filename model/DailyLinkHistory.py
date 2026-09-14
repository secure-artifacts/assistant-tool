from datetime import date, datetime, timedelta
from typing import Dict, Mapping, Optional, Tuple


DAILY_LINK_HISTORY_CONFIG_KEY = "daily_drive_link_history"
DAILY_LINK_HISTORY_RETENTION_DAYS = 7
DAILY_TASK_SHEET_REASON_MAX_LENGTH = 240


def _date_value(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def normalize_daily_link_history(
    raw_history,
    today: Optional[date] = None,
) -> Dict[str, Dict]:
    """Validate history and keep only the most recent seven calendar days."""
    current_day = _date_value(today) or date.today()
    first_day = current_day - timedelta(days=DAILY_LINK_HISTORY_RETENTION_DAYS - 1)
    if not isinstance(raw_history, Mapping):
        return {}

    normalized = {}
    for raw_day, raw_day_entry in raw_history.items():
        day = _date_value(raw_day)
        if day is None or day < first_day or day > current_day:
            continue
        if not isinstance(raw_day_entry, Mapping):
            continue
        raw_people = raw_day_entry.get("people", {})
        if not isinstance(raw_people, Mapping):
            raw_people = {}

        people = {}
        for raw_person, raw_slots in raw_people.items():
            person = str(raw_person).strip()
            if not person or not isinstance(raw_slots, Mapping):
                continue
            slots = {}
            for raw_slot, raw_link_entry in raw_slots.items():
                slot = str(raw_slot).strip() or "未标记"
                if isinstance(raw_link_entry, Mapping):
                    link = str(raw_link_entry.get("link", "")).strip()
                    saved_at = str(raw_link_entry.get("saved_at", "")).strip()
                else:
                    link = str(raw_link_entry).strip()
                    saved_at = ""
                if not link:
                    continue
                slots[slot] = {
                    "link": link,
                    "saved_at": saved_at,
                }
            if slots:
                people[person] = slots

        task_sheet_failures = {}
        raw_failures = raw_day_entry.get("task_sheet_failures", {})
        if isinstance(raw_failures, Mapping):
            for raw_slot, raw_slot_failures in raw_failures.items():
                slot = str(raw_slot).strip() or "未标记"
                if not isinstance(raw_slot_failures, Mapping):
                    continue
                slot_failures = {}
                for raw_file_name, raw_failure in raw_slot_failures.items():
                    file_name = str(raw_file_name).strip()
                    if not file_name:
                        continue
                    if isinstance(raw_failure, Mapping):
                        reason = str(raw_failure.get("reason", "")).strip()[
                            :DAILY_TASK_SHEET_REASON_MAX_LENGTH
                        ]
                        saved_at = str(raw_failure.get("saved_at", "")).strip()
                    else:
                        reason = str(raw_failure).strip()[
                            :DAILY_TASK_SHEET_REASON_MAX_LENGTH
                        ]
                        saved_at = ""
                    slot_failures[file_name] = {
                        "reason": reason,
                        "saved_at": saved_at,
                    }
                if slot_failures:
                    task_sheet_failures[slot] = slot_failures

        if people or task_sheet_failures:
            normalized[day.isoformat()] = {
                "updated_at": str(raw_day_entry.get("updated_at", "")).strip(),
                "people": people,
                "task_sheet_failures": task_sheet_failures,
            }

    return dict(sorted(normalized.items()))


def record_daily_person_links(
    history,
    upload_date,
    upload_slot,
    person_links,
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Dict], int]:
    """Merge one successful upload batch into the rolling history."""
    moment = now or datetime.now()
    day = _date_value(upload_date)
    if day is None:
        raise ValueError(f"无法识别链接归档日期：{upload_date}")
    if not isinstance(person_links, Mapping):
        return normalize_daily_link_history(history, moment.date()), 0

    merged = normalize_daily_link_history(history, moment.date())
    first_day = moment.date() - timedelta(days=DAILY_LINK_HISTORY_RETENTION_DAYS - 1)
    if day < first_day or day > moment.date():
        return merged, 0
    day_key = day.isoformat()
    slot = str(upload_slot).strip() or "未标记"
    saved_at = moment.isoformat(timespec="seconds")
    day_entry = merged.setdefault(
        day_key,
        {"updated_at": saved_at, "people": {}, "task_sheet_failures": {}},
    )
    people = day_entry.setdefault("people", {})

    saved_count = 0
    for raw_person, raw_link in person_links.items():
        person = str(raw_person).strip()
        link = str(raw_link).strip()
        if not person or not link:
            continue
        people.setdefault(person, {})[slot] = {
            "link": link,
            "saved_at": saved_at,
        }
        saved_count += 1
    if saved_count:
        day_entry["updated_at"] = saved_at

    return normalize_daily_link_history(merged, moment.date()), saved_count


def _result_file_name(value) -> str:
    if isinstance(value, Mapping):
        return str(value.get("file_name", "")).strip()
    return str(value or "").strip()


def update_daily_task_sheet_results(
    history,
    upload_date,
    upload_slot,
    failed_files,
    successful_files=(),
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Dict], int]:
    """Update unresolved task-submission-sheet failures for one upload run."""
    moment = now or datetime.now()
    day = _date_value(upload_date)
    if day is None:
        raise ValueError(f"无法识别任务表记录日期：{upload_date}")
    merged = normalize_daily_link_history(history, moment.date())
    first_day = moment.date() - timedelta(days=DAILY_LINK_HISTORY_RETENTION_DAYS - 1)
    if day < first_day or day > moment.date():
        return merged, 0

    normalized_failures = []
    for failure in failed_files or ():
        file_name = _result_file_name(failure)
        if not file_name:
            continue
        reason = (
            str(failure.get("reason", "")).strip()[
                :DAILY_TASK_SHEET_REASON_MAX_LENGTH
            ]
            if isinstance(failure, Mapping)
            else ""
        )
        normalized_failures.append((file_name, reason))
    successful_names = {
        _result_file_name(item)
        for item in (successful_files or ())
        if _result_file_name(item)
    }

    day_key = day.isoformat()
    day_entry = merged.get(day_key)
    if day_entry is None and not normalized_failures:
        return merged, 0
    saved_at = moment.isoformat(timespec="seconds")
    day_entry = merged.setdefault(
        day_key,
        {"updated_at": saved_at, "people": {}, "task_sheet_failures": {}},
    )
    failures_by_slot = day_entry.setdefault("task_sheet_failures", {})

    # A later verified write resolves the same file even if the selected batch changed.
    if successful_names:
        for existing_slot in list(failures_by_slot):
            slot_failures = failures_by_slot[existing_slot]
            for file_name in successful_names:
                slot_failures.pop(file_name, None)
            if not slot_failures:
                failures_by_slot.pop(existing_slot, None)

    slot = str(upload_slot).strip() or "未标记"
    slot_failures = failures_by_slot.setdefault(slot, {})
    for file_name, reason in normalized_failures:
        slot_failures[file_name] = {
            "reason": reason,
            "saved_at": saved_at,
        }
    if not slot_failures:
        failures_by_slot.pop(slot, None)

    if normalized_failures or successful_names:
        day_entry["updated_at"] = saved_at
    if not day_entry.get("people") and not failures_by_slot:
        merged.pop(day_key, None)

    return normalize_daily_link_history(merged, moment.date()), len(normalized_failures)


def history_dates(history):
    return sorted(normalize_daily_link_history(history).keys(), reverse=True)


def daily_link_counts(history, day_key) -> Tuple[int, int]:
    normalized = normalize_daily_link_history(history)
    people = normalized.get(str(day_key), {}).get("people", {})
    return len(people), sum(len(slots) for slots in people.values())


def daily_task_sheet_failures(history, day_key):
    normalized = normalize_daily_link_history(history)
    failures_by_slot = normalized.get(str(day_key), {}).get(
        "task_sheet_failures",
        {},
    )
    failures = []
    for slot, slot_failures in sorted(failures_by_slot.items()):
        for file_name, entry in sorted(slot_failures.items()):
            failures.append({
                "slot": slot,
                "file_name": file_name,
                "reason": str(entry.get("reason", "")).strip(),
                "saved_at": str(entry.get("saved_at", "")).strip(),
            })
    return failures


def daily_task_sheet_failure_count(history, day_key) -> int:
    return len(daily_task_sheet_failures(history, day_key))


def format_daily_task_sheet_failures(history, day_key) -> str:
    failures = daily_task_sheet_failures(history, day_key)
    return "\n".join(
        f"[{item['slot']}] {item['file_name']}"
        + (f" —— {item['reason']}" if item["reason"] else "")
        for item in failures
    )


def format_person_daily_links(person, slots) -> str:
    lines = [f"{person}："]
    for slot, entry in sorted(slots.items()):
        link = str(entry.get("link", "")).strip()
        if link:
            lines.append(f"批次 {slot}：{link}")
    return "\n".join(lines)


def format_daily_links(history, day_key) -> str:
    normalized = normalize_daily_link_history(history)
    people = normalized.get(str(day_key), {}).get("people", {})
    day = _date_value(day_key)
    heading = (
        f"{day.year:04d}年{day.month:02d}月{day.day:02d}日"
        if day
        else str(day_key)
    )
    sections = [heading]
    for person, slots in sorted(people.items()):
        sections.append(format_person_daily_links(person, slots))
    return "\n\n".join(sections)
