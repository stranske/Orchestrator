# ruff: noqa: F821, F841
# Production controller pure functions/main contract; injected external boundaries only.
# Source SHA256: e7bf8a31b127caf4236eb753fac881042abf573e098cab57743bc78d9cf4dc56
# fmt: off
QUERY = 'query($q:String!,$after:String){search(query:$q,type:ISSUE,first:100,after:$after){issueCount pageInfo{hasNextPage endCursor} nodes{... on PullRequest{url title number repository{nameWithOwner} author{login __typename} headRefName headRefOid closingIssuesReferences(first:20){nodes{number repository{nameWithOwner}}} isDraft labels(first:30){nodes{name}} body createdAt updatedAt mergeStateStatus reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved isOutdated}} commits(last:1){nodes{commit{committedDate tree{oid} statusCheckRollup{state contexts(first:100){pageInfo{hasNextPage} nodes{... on CheckRun{name status conclusion} ... on StatusContext{context state}}}}}}}}}}}'

def signals(pr):
    threads = pr.get("reviewThreads") or {}
    commits = ((pr.get("commits") or {}).get("nodes") or [])
    commit = commits[-1].get("commit", {}) if commits else {}
    rollup = commit.get("statusCheckRollup") or {}
    contexts = rollup.get("contexts") or {}
    nodes = contexts.get("nodes") or []
    failed = any(n.get("conclusion") in {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
                 or n.get("state") in {"FAILURE", "ERROR"} for n in nodes)
    pending = any(n.get("status") in {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"}
                  or n.get("state") == "PENDING" for n in nodes)
    active_threads = sum(not n.get("isResolved") and not n.get("isOutdated") for n in threads.get("nodes", []))
    incomplete = bool((threads.get("pageInfo") or {}).get("hasNextPage") or (contexts.get("pageInfo") or {}).get("hasNextPage"))
    return {"failed": failed, "pending": pending, "threads": active_threads,
            "incomplete": incomplete, "check_state": rollup.get("state", "UNKNOWN"),
            "pushed_at": commit.get("committedDate"),
            "checks": sorted((n.get("name") or n.get("context") or "", n.get("status") or "", n.get("conclusion") or n.get("state") or "") for n in nodes)}

def fingerprint(pr, sig):
    # Ignore discussion timestamps: controller commentary must not manufacture new work.
    value = [pr.get("headRefOid"), pr.get("isDraft"), sorted(inventory.labels(pr)),
             pr.get("body"), pr.get("mergeStateStatus"), sig]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

def build_routes(prs, guard, state, terminal, now, held=set(), challenge=set(), retry=set(), running_lanes=None):
    routes = {k: [] for k in LANES}
    observations = {}
    waits = []
    ignored = []
    prior = state.get("targets", {})
    coverage = terminal.get("coverage_json") or {}
    running_lanes = running_lanes or set()
    if not isinstance(coverage, dict):
        coverage = {}
    for pr in prs:
        target = f"{inventory.repo(pr)}#{pr['number']}"
        lane, action, sig = classify(pr, held, challenge, retry, now)
        fp = fingerprint(pr, sig)
        old = prior.get(target, {})
        since = old.get("unchanged_since", now) if old.get("fingerprint") == fp else now
        row = {"target": target, "head": pr.get("headRefOid"), "fingerprint": fp,
               "action": action, "due": inventory.iso_now(), "lane": lane,
               "delivery_owner": pr_owner(pr), "execution_state": lane if lane in {'held', 'waiting'} else 'active',
               "unchanged_since": since}
        # A terminal result is evidence for this fingerprint, not a timer to repeat diagnosis.
        result = coverage.get(target) or {}
        diagnosed = old.get("diagnosed_fingerprint")
        if result.get("head") == row["head"] and result.get("fingerprint") == fp and result.get("execution_error") is None:
            diagnosed = fp
        if terminal.get("selected_target") == target and terminal.get("execution_error") is None:
            selected = (terminal.get("inventory_evidence") or {}).get("selected") or {}
            if selected.get("head_sha") == row["head"] and terminal.get("completed_at_utc"):
                completed = inventory.parse_iso(terminal["completed_at_utc"])
                # Adopt only a result newer than our observation, or one explicitly fingerprinted.
                if result.get("fingerprint") == fp or (old.get("fingerprint") == fp and completed and completed >= old.get("observed_at", now)):
                    diagnosed = fp
        # Productive recovery can create a real due follow-up; a missed due time
        # must be eligible again even if the PR head has not moved yet.
        due = inventory.parse_iso(result.get("due") or "")
        if result.get("fingerprint") == fp and (
            result.get("execution_error") or
            (result.get("outcome_class") == "productive_recovery" and due is not None and now >= due)
        ):
            diagnosed = None
        row["diagnosed_fingerprint"] = diagnosed
        row["observed_at"] = now
        observations[target] = row
        if lane == "held" or (diagnosed == fp and result.get("park_until_material_change")):
            row['execution_state'] = 'held'
            ignored.append(row)
            continue
        if lane == "escalator":
            if diagnosed != fp:
                routes[lane].append(row)
            else:
                ignored.append(dict(row, action="Diagnosed unchanged blocker; await material evidence"))
            continue
        created = inventory.parse_iso(pr.get("createdAt") or "")
        acquisition_over = created is not None and now - created >= 30 * 60
        receipt = (state.get('lane_receipts') or {}).get(lane) or {}
        stopped_at = inventory.parse_iso(receipt.get('at') or '')
        owner_stopped = (lane in routes and lane not in running_lanes and
                         receipt.get('outcome') == 'no_op' and stopped_at is not None and
                         stopped_at >= since + STALL_SECONDS)
        pending_stuck = lane == 'waiting' and sig['pending'] and now - since >= STALL_SECONDS
        missed_recovery = (result.get('fingerprint') == fp and result.get('outcome_class') == 'productive_recovery'
                           and result.get('action') and due is not None and now >= due and lane not in running_lanes)
        if acquisition_over and diagnosed != fp and (missed_recovery or (now - since >= STALL_SECONDS and (owner_stopped or pending_stuck))):
            routes["escalator"].append(dict(row, lane="escalator", ordinary_lane=lane,
                eligibility_reason='missed_concrete_recovery_deadline' if missed_recovery else ('owner_reported_no_action_after_deadline' if owner_stopped else 'same_pending_checks_observed_for_three_hours'),
                action=('Reconcile the missed concrete recovery: ' + result['action']) if missed_recovery else ("Recover the ordinary owner's documented no-action stall" if owner_stopped else "Diagnose the exact-head checks proven stuck pending for three hours")))
        if lane == "waiting":
            waits.append(row)
        elif lane in routes:
            routes[lane].append(row)
        # Maintenance and its registered repairs keep the same source owner.
    for item in guard.get("actionable_candidates", []):
        lane = issue_owner(item)
        routes[lane].append({"target": f"{item['repository']}#{item['number']}",
            "action": item.get("next_automatic_action") or "Select the next bounded issue action",
            "fingerprint": item.get("updatedAt"), "lane": lane})
    for item in guard.get("challenge_due", []):
        target = f"{item['repository']}#{item['number']}"
        fp = item.get("updatedAt")
        old = prior.get(target, {})
        result = coverage.get(target, {})
        diagnosed = old.get("diagnosed_fingerprint")
        if result.get("fingerprint") == fp and not result.get("execution_error"):
            diagnosed = fp
        row = {"target": target, "fingerprint": fp, "lane": "escalator",
               "action": item.get("next_automatic_action") or "Independently challenge the claimed human blocker",
               "observed_at": now, "diagnosed_fingerprint": diagnosed}
        observations[target] = row
        if diagnosed != fp:
            routes["escalator"].append(row)
    # A scalar-only guard cannot prove absence of actionable candidates.
    if guard.get("actionable_count", 0) and not guard.get("actionable_candidates"):
        routes["opener"].append({"target": "opener-queue", "action": "Reconcile actionable liveness count", "lane": "opener"})
    return routes, observations, waits, ignored

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", choices=["issue-active"])
    parser.add_argument("--source", default="manual")
    parser.add_argument("--update-args", choices=list(LANES))
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--verify-and-observe", action="store_true")
    parser.add_argument("--relay-eligible", choices=["opener", "closer"])
    parser.add_argument("--record-human-alert", action="store_true")
    args = parser.parse_args(argv)
    if args.relay_eligible:
        return 0 if relay_eligible(args.relay_eligible) else 1
    if args.update_args:
        plan = load(PLAN, {})
        if time.time() - plan.get("created_at", 0) > 10 * 60:
            raise RuntimeError("Plan expired; collect fresh evidence before updating")
        print(json.dumps(update_args(args.update_args, plan["desired"][args.update_args])))
        return 0
    if args.verify or args.verify_and_observe:
        plan = load(PLAN, {})
        if set(plan.get('desired', {})) != set(LANES) or time.time() - plan.get('created_at', 0) > 10 * 60:
            raise RuntimeError('Missing or expired five-lane plan; cannot claim activation verification')
        actual = app_settings()
        mismatches = {k: {"expected": v, "actual": actual[k]} for k, v in plan.get("desired", {}).items()
                      if any(actual[k].get(field) != value for field, value in v.items())}
        save(ROOT / "app-readback.json", {"verified_at": inventory.iso_now(), "actual": actual, "mismatches": mismatches})
        if args.verify_and_observe:
            import followthrough_observer
            observation = followthrough_observer.observe()
            compact = {'observed_at': observation['observed_at'], 'errors': observation['errors'],
                       'lanes': {lane: {
                           'actual_status': row['actual_status'],
                           'pause_disposition': row['pause_disposition'],
                           'run_start_count': row['run_start_count'],
                           'activation_status': row['actual_activation_then_run']['status'],
                           'completed_cycle_count': len(row['actual_activation_then_run'].get('completed_cycles', []))
                       } for lane, row in observation['lanes'].items()}}
            context = followthrough_context(plan)
            print(json.dumps({'verified': not mismatches, 'mismatches': mismatches,
                              'observation': compact, 'followthrough': context}))
            return int(bool(mismatches or observation['errors'] or
                            context and context.get('errors')))
        print(json.dumps({"verified": not mismatches, "mismatches": mismatches, "actual": actual}))
        return int(bool(mismatches))
    with (ROOT / ".demand.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load(STATE, {})
        if args.record_human_alert:
            plan = load(PLAN, {})
            if time.time() - plan.get("created_at", 0) > 10 * 60:
                raise RuntimeError("Alert plan expired")
            alerts = [r for r in plan.get('human_deadline_due', []) if r.get('new_alert_due') and
                      time.time() - ((state.get('accountability') or {}).get(r['target']) or {}).get('last_human_alert_at', 0) >= accountability.REMINDER_SECONDS]
            for row in alerts:
                entry = (state.get('accountability') or {}).get(row['target'])
                if entry and entry.get('episode') == row['episode']:
                    entry['last_human_alert_at'] = time.time()
            repair_alerts = [r for r in plan.get('repair_budget_decisions', [])
                             if r.get('new_alert_due') and (r['request_id'] not in state.get('repair_budget_alerts', {}) or
                             time.time() - state['repair_budget_alerts'][r['request_id']] >= accountability.REMINDER_SECONDS)]
            for row in repair_alerts:
                state.setdefault('repair_budget_alerts', {})[row['request_id']] = time.time()
            alerts.extend(repair_alerts)
            if alerts:
                save(ROOT / 'human-alert-latest.json', {'prepared_at': inventory.iso_now(), 'items': alerts,
                    'instruction': 'Present these exact targets and human actions in the final controller response. This record is not proof of notification delivery.'})
            save(STATE, state)
            print(json.dumps({'prepared_human_alert_count': len(alerts), 'artifact': str(ROOT / 'human-alert-latest.json')}))
            return 0
        if args.event:
            # Postrun records progress; schedule authority stays with the Luna controller.
            state["last_progress_event"] = {"at": inventory.iso_now(), "source": args.source[:500]}
            lane = re.search(r'\blane=(opener|closer)\b', args.source)
            outcome = re.search(r'\boutcome=([a-z_]+)\b', args.source)
            if lane and outcome:
                state.setdefault('lane_receipts', {})[lane.group(1)] = {
                    'at': inventory.iso_now(), 'outcome': outcome.group(1), 'source': args.source[:500]}
            save(STATE, state)
            print(json.dumps({"mode": "event", "recorded": True, "schedule_changes": 0}))
            return 0
        actual = app_settings()
        prs, issues, guard, errors = collect()
        warnings = []
        now = time.time()
        held, challenge, retry = inventory.scoped_blocker_states()
        terminal = load(inventory.ROOT / "reviewed-repo-stall-escalator/memory.md", {})
        running = running_lanes()
        running_owners = running_lane_owners(running)
        stale_lane_owners = {lane: [owner for owner in owners if owner.get('classification') == 'stale']
                             for lane, owners in running_owners.items()}
        stale_lane_owners = {lane: owners for lane, owners in stale_lane_owners.items() if owners}
        live_running_owners = {lane: [owner for owner in owners if owner.get('classification') != 'stale']
                               for lane, owners in running_owners.items()}
        live_running_owners = {lane: owners for lane, owners in live_running_owners.items() if owners}
        routes, observations, waits, ignored = build_routes(
            (prs or {}).get("nodes", []), guard or {}, state, terminal, now, held, challenge, retry, running)
        budget_decisions = []
        ownership, ownership_repairs = {}, []
        if not errors:
            ownership, ownership_repairs = reconcile_ownership(
                prs['nodes'], issues['nodes'], guard or {}, routes, observations, now)
            try:
                view = maintenance.budget.summary()
                budget_decisions = maintenance_admissions(prs['nodes'], routes, observations, waits,
                    maintenance.read_control(), view, now)
                for row in waits:
                    target = row['target']
                    if target not in ownership:
                        continue
                    state_name = ('controller-observation' if row.get('execution_state') == 'controller-observation' else
                                  'waiting' if target.lower() in {maintenance.identity(n) for n in prs['nodes']} else 'repair-wait')
                    ownership[target].update(state=state_name, action=row['action'])
                    observations[target].update(execution_state=state_name, action=row['action'])
                for target, owned in ownership.items():
                    via = owned.get('via') or []
                    if via and all(ownership[p]['state'] != 'active' for p in via):
                        owned.update(state=ownership[via[0]]['state'], action=ownership[via[0]]['action'])
                        observations[target].update(execution_state=owned['state'], action=owned['action'])
            except (ValueError, OSError, KeyError, TypeError) as exc:
                errors.append('Maintenance admission unavailable: ' + str(exc))
        records, recoveries, human_due, exclusions = accountability.evaluate(
            (prs or {}).get('nodes', []), (issues or {}).get('nodes', []), guard or {},
            state.get('accountability', {}), terminal.get('coverage_json') or {}, observations, now)
        gaps = ownership_gaps(records, ownership, routes, observations) if not errors else []
        warnings.extend(ownership_coverage_warnings(gaps))
        if errors:
            human_due = accountability.unknown_due(state.get('accountability', {}), now,
                                                  state.get('last_complete_at'), errors)
        # Annotate real work with deadline context. The ledger never invents
        # Sol work from age, uncertainty, a draft, or a human-only hold.
        for row in routes['escalator']:
            record = records.get(row['target'])
            if record:
                row.update(accountability_episode=record['episode'], kind=record['kind'], human_due_at=record['human_due_at'])
        routes['escalator'].sort(key=lambda r: (r.get('human_due_at', now + accountability.HUMAN_SECONDS), r['target']))
        # Incomplete discovery never deactivates a lane or wakes Sol just to retry discovery.
        repo_maintenance = {'assignments': [], 'errors': []}
        if not errors:
            try:
                repo_maintenance = repo_maintenance_admissions(routes, waits,
                    maintenance.read_control(), view, maintenance.existing_worker(), now, live_running_owners)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                errors.append('Repo maintenance planning unavailable: ' + str(exc))
        freeze = error_freeze(errors, state, now)
        effective_errors = [] if freeze['lapsed'] else errors
        desired = desired_settings(routes, actual, effective_errors, live_running_owners)
        updates = [lane for lane in LANES if any(actual[lane].get(k) != v for k, v in desired[lane].items())]
        plan = {"created_at": now, "generated_at": inventory.iso_now(), "errors": errors,
                "warnings": warnings, "error_freeze": error_freeze_message(freeze),
                "lanes_frozen_by_errors": frozen_lanes_message(freeze),
                "desired": desired, "updates": updates, "routes": routes, "waiting": waits,
                "held": ignored, "pr_count": (prs or {}).get("issueCount"),
                "issue_count": (issues or {}).get('issueCount'),
                "accountability_policy": {'recovery_hours': 3, 'human_after_days': 7, 'reminder_hours': 24},
                "accountability_count": len(records), "deadline_exclusions": exclusions,
                "human_deadline_due": human_due,
                "human_deadline_notification_required": any(r['new_alert_due'] for r in human_due),
                "guard_counts": {k: v for k, v in (guard or {}).items() if isinstance(v, (int, bool))},
                "human_attention": (guard or {}).get("human_attention_samples", [])}
        budget_alerts(budget_decisions, state, now)
        plan['repo_maintenance'] = repo_maintenance
        plan['repair_budget_decisions'] = budget_decisions
        plan['repair_budget_notification_required'] = any(r['new_alert_due'] for r in budget_decisions)
        plan['maintenance_running_owner'] = maintenance.existing_worker()
        plan['running_lane_owners'] = live_running_owners
        plan['stale_lane_owners'] = stale_lane_owners
        plan.update(delivery_ownership=ownership if not errors else state.get('delivery_ownership', {}),
                    ownership_complete=not errors, ownership_gap_targets=gaps,
                    ownership_repaired_targets=ownership_repairs if not errors else [],
                    ownership_snapshot_at=plan['generated_at'] if not errors else state.get('last_complete_at'))
        save(PLAN, plan)
        write_operational_memory(plan)
        # Workers read their bounded assignment rather than repeat controller discovery.
        if not errors:
            for lane, rows in routes.items():
                save(ROOT / (lane + "-assignment.json"), {"generated_at": plan["generated_at"],
                    "model": desired[lane]["model"], "reasoning_effort": desired[lane]["reasoning_effort"],
                    "targets": rows, "errors": errors})
            state["targets"] = observations
            state['accountability'] = records
            state['delivery_ownership'] = ownership
            state["last_complete_at"] = plan["generated_at"]
            state.pop('error_freeze', None)
            save(STATE, state)
        else:
            state['error_freeze'] = {'errors': freeze['errors'], 'first_seen': freeze['first_seen']}
            save(STATE, state)
        compact = {k: plan[k] for k in ["generated_at", "errors", "warnings", "error_freeze",
                   "lanes_frozen_by_errors", "updates", "desired", "pr_count", "guard_counts"]}
        compact["route_counts"] = {k: len(v) for k, v in routes.items()}
        compact["waiting_count"] = len(waits)
        compact["held_count"] = len(ignored)
        compact["plan_path"] = str(PLAN)
        compact['accountability_count'] = len(records) if not errors else None
        compact['human_deadline_overdue_count'] = len(human_due) if not errors else None
        compact['human_deadline_notification_required'] = plan['human_deadline_notification_required']
        compact['human_deadline_alert_path'] = str(PLAN)
        compact['accountability_policy'] = plan['accountability_policy']
        compact['ownership_complete'] = plan['ownership_complete']
        compact['ownership_gap_count'] = len(gaps) if not errors else None
        compact['ownership_repaired_targets'] = plan['ownership_repaired_targets']
        compact['repair_budget_decisions'] = budget_decisions
        compact['repair_budget_notification_required'] = plan['repair_budget_notification_required']
        compact['maintenance_running_owner'] = plan['maintenance_running_owner']
        compact['running_lane_owners'] = live_running_owners
        compact['stale_lane_owners'] = stale_lane_owners
        compact['running_lane_owner_count'] = sum(len(owners) for owners in live_running_owners.values())
        compact['stale_lane_owner_count'] = sum(len(owners) for owners in stale_lane_owners.values())
        print(json.dumps(compact), flush=True)
        with (ROOT / "demand-ledger.jsonl").open("a") as stream:
            stream.write(json.dumps(compact) + "\n")
        return 0
# fmt: on
