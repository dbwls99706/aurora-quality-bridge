"""Read-only, quantity-conserving supply alternatives. No AI or persistence writes."""
import math
from datetime import date


class SupplyAlternativeError(ValueError):
    pass


def compare_supply(service, db, case, total_quantity, early_quantity, allocations, add_days):
    batch = service.evidence.batches[case['batch_id']]
    service._validate_conditions({'replacement_qty': total_quantity}, batch['produced_qty'])
    if total_quantity is None:
        raise SupplyAlternativeError('전체 모의 수량을 1 EA 이상 입력해 주세요. 확정 불량량을 재공급 필요량으로 자동 사용하지 않습니다.')
    if type(early_quantity) is not int or not 1 <= early_quantity < total_quantity:
        raise SupplyAlternativeError('선공급 수량은 1 EA 이상, 전체 모의 수량 미만의 정수여야 합니다. 잔량을 0으로 없앨 수 없습니다.')
    remaining_quantity = total_quantity - early_quantity
    required_ids = service._affected_customer_ids(case)
    if not isinstance(allocations, list) or len(allocations) > len(required_ids):
        raise SupplyAlternativeError('고객별 배분 목록을 확인해 주세요.')
    entries = {}
    allowed = {'customer_id', 'required_quantity', 'due_date', 'early_quantity', 'remaining_quantity'}
    for row in allocations:
        if not isinstance(row, dict) or set(row) != allowed:
            raise SupplyAlternativeError('고객 배분에는 고객 ID, 모의 필요량·기한, 선공급·잔량 배분만 입력할 수 있습니다. 승인 정보는 입력할 수 없습니다.')
        cid = row['customer_id']
        if not isinstance(cid, str) or cid not in required_ids or cid in entries:
            raise SupplyAlternativeError('영향 고객 목록의 고객을 중복 없이 입력해 주세요.')
        need = row['required_quantity']
        if need is not None and (type(need) is not int or not 0 <= need <= batch['produced_qty']):
            raise SupplyAlternativeError('고객 모의 필요량은 빈 값(미확인) 또는 배치 생산량 이하의 0 이상 정수여야 합니다.')
        for key in ('early_quantity', 'remaining_quantity'):
            if type(row[key]) is not int or not 0 <= row[key] <= total_quantity:
                raise SupplyAlternativeError('고객별 배분은 전체 모의 수량 이하의 0 이상 정수여야 합니다.')
        if need is not None and row['early_quantity'] + row['remaining_quantity'] > need:
            raise SupplyAlternativeError('고객 배분 수량이 입력한 고객 모의 필요량을 넘습니다.')
        if row['due_date'] is not None:
            service._validate_conditions({'customer_due_date': row['due_date']}, batch['produced_qty'])
            service._validate_schedule_dates({**case['conditions'], 'customer_due_date': row['due_date']})
        entries[cid] = dict(row)
    for cid in required_ids:
        entries.setdefault(cid, {'customer_id': cid, 'required_quantity': None, 'due_date': None,
                                 'early_quantity': 0, 'remaining_quantity': 0})
    allocated_early = sum(e['early_quantity'] for e in entries.values())
    allocated_remaining = sum(e['remaining_quantity'] for e in entries.values())
    if allocated_early > early_quantity or allocated_remaining > remaining_quantity:
        raise SupplyAlternativeError('선공급 또는 잔량의 고객 배분 합계가 해당 공급분 수량을 넘습니다.')

    conditions = {**case['conditions'], 'replacement_qty': total_quantity,
                  'inventory_verified': False, 'production_confirmed': False, 'qa_release': False}
    service._validate_schedule_dates(conditions)
    candidate_case = {**case, 'conditions': conditions}
    full = service._plan(batch, conditions)
    capacity = conditions['production_daily_capacity']
    early_days = math.ceil(early_quantity / capacity)
    remaining_days = math.ceil(remaining_quantity / capacity)
    lead = conditions['production_lead_days'] + conditions['production_delay_days']
    qa = conditions['qa_days']
    effective = conditions['effective_date']
    first_ship = add_days(effective, lead + early_days + qa)
    final_ship = add_days(effective, lead + early_days + qa + remaining_days + qa)
    split_plan = {**full, 'production_days': early_days + remaining_days,
                  'production_end_date': add_days(effective, lead + early_days + qa + remaining_days),
                  'earliest_ship_date': final_ship,
                  'delay_days': max(0, (date.fromisoformat(final_ship) - date.fromisoformat(conditions['customer_due_date'])).days)}
    unallocated_early = early_quantity - allocated_early
    unallocated_remaining = remaining_quantity - allocated_remaining
    known_need = sum(e['required_quantity'] or 0 for e in entries.values())
    unknown_need = sum(e['required_quantity'] is None for e in entries.values())
    unknown_due = sum(e['due_date'] is None and e['required_quantity'] != 0 for e in entries.values())
    unmet_known = sum(max(0, e['required_quantity'] - e['early_quantity'] - e['remaining_quantity'])
                      for e in entries.values() if e['required_quantity'] is not None)
    scope = service._scope_blockers(db, candidate_case)
    checks = ['영업팀: 실제 고객 보유량·필요량과 전체 수량 대조',
              '생산팀: 실제 일일 능력·원료·예약 충돌과 분할 일정 확인',
              '품질팀: 각 공급분의 검사·출하 승인',
              '영업팀: 고객별 수량·기한·분할 수령 동의 확인']
    information = []
    if unallocated_early + unallocated_remaining:
        information.append(f'전체 {unallocated_early + unallocated_remaining:,} EA 고객 미배정')
    if unknown_need:
        information.append(f'{unknown_need}개 고객 필요량 미확인')
    if unknown_due:
        information.append(f'{unknown_due}개 고객 기한 미확인')
    if unmet_known:
        information.append(f'입력된 고객 필요량 중 {unmet_known:,} EA 미충족')
    if not unknown_need and known_need != total_quantity:
        information.append(f'고객 모의 필요량 합계 {known_need:,} EA와 전체 모의 수량 {total_quantity:,} EA 불일치')

    def result(plan_id, name, plan, lots):
        constraints = []
        if not plan['capacity_snapshot']:
            constraints.append('원본 생산능력 자료 없음')
        if capacity > plan['daily_capacity_limit']:
            constraints.append(f"모의 일일 수량 {capacity:,} EA가 보수적 일일 잔여능력 {plan['daily_capacity_limit']:,} EA 초과")
        if total_quantity > plan['residual_capacity_qty']:
            constraints.append(f"전체 모의 수량 {total_quantity:,} EA가 원본 스냅샷 잔여능력 {plan['residual_capacity_qty']:,} EA 초과")
        constraints.extend(service._reservation_blockers(db, candidate_case, plan))
        customers, late_total, on_time_total, unknown_deadline_qty = [], 0, 0, 0
        for cid, entry in entries.items():
            shipments = ([{'quantity': entry['early_quantity'] + entry['remaining_quantity'], 'ship_date': lots[0]['ship_date']}]
                         if plan_id == 'full_later' else
                         [{'quantity': entry['early_quantity'], 'ship_date': lots[0]['ship_date']},
                          {'quantity': entry['remaining_quantity'], 'ship_date': lots[1]['ship_date']}])
            shipments = [s for s in shipments if s['quantity'] > 0]
            due = entry['due_date']
            late_qty, on_time_qty = 0, 0
            for shipment in shipments:
                shipment['delay_calendar_days'] = max(0, (date.fromisoformat(shipment['ship_date']) - date.fromisoformat(due)).days) if due else None
                if due is None:
                    unknown_deadline_qty += shipment['quantity']
                elif shipment['delay_calendar_days']:
                    late_qty += shipment['quantity']
                else:
                    on_time_qty += shipment['quantity']
            assigned = entry['early_quantity'] + entry['remaining_quantity']
            need = entry['required_quantity']
            customers.append({**entry, 'customer_name': service.evidence.customers[cid]['customer_name'],
                'assigned_quantity': assigned, 'unmet_quantity': max(0, need - assigned) if need is not None else None,
                'shipments': shipments, 'on_time_quantity': on_time_qty if due else None,
                'late_quantity': late_qty if due else None,
                'max_delay_calendar_days': max((s['delay_calendar_days'] for s in shipments), default=None) if due else None,
                'fulfillment_status': 'unknown_need' if need is None else ('unmet' if assigned < need else 'assumed_allocated'),
                'approval_status': 'not_confirmed'})
            late_total += late_qty
            on_time_total += on_time_qty
        if plan['delay_days']:
            constraints.append(f"전체 잔량 완료가 저장된 공통 희망일보다 {plan['delay_days']}달력일 늦음")
        if late_total:
            constraints.append(f'입력된 고객별 기한보다 늦은 배정 물량 {late_total:,} EA')
        return {'id': plan_id, 'name': name, 'quantity': total_quantity, 'lots': lots,
                'production_days': plan['production_days'], 'first_ship_date': lots[0]['ship_date'],
                'final_ship_date': lots[-1]['ship_date'], 'common_due_date': conditions['customer_due_date'],
                'final_delay_calendar_days': plan['delay_days'], 'customers': customers,
                'allocated_quantity': allocated_early + allocated_remaining,
                'unallocated_quantity': unallocated_early + unallocated_remaining,
                'on_time_quantity': on_time_total, 'late_quantity': late_total,
                'unknown_deadline_quantity': unknown_deadline_qty,
                'calculated_constraints': list(dict.fromkeys(constraints)),
                'missing_information': information + scope,
                'approvals_required': checks, 'approval_status': 'not_confirmed',
                'feasible': False, 'physical_supply_ready': False, 'customer_agreement': 'pending',
                'external_execution': False}

    full_result = result('full_later', '전량 후공급', full,
                         [{'id': 'full', 'quantity': total_quantity, 'production_days': full['production_days'],
                           'ship_date': full['earliest_ship_date'], 'unallocated_quantity': unallocated_early + unallocated_remaining}])
    split_result = result('partial_then_remaining', '일부 선공급 + 잔량 후공급', split_plan,
                          [{'id': 'early', 'quantity': early_quantity, 'production_days': early_days,
                            'ship_date': first_ship, 'unallocated_quantity': unallocated_early},
                           {'id': 'remaining', 'quantity': remaining_quantity, 'production_days': remaining_days,
                            'ship_date': final_ship, 'unallocated_quantity': unallocated_remaining}])
    return {'case_id': case['id'], 'version': case['version'], 'kind': 'read_only_simulation',
            'quantity_basis': 'user_entered_simulation_not_verified_replacement_demand',
            'total_quantity': total_quantity, 'early_quantity': early_quantity, 'remaining_quantity': remaining_quantity,
            'known_customer_demand_quantity': known_need, 'unknown_customer_demand_count': unknown_need,
            'quantity_conserved': all(sum(lot['quantity'] for lot in r['lots']) == total_quantity and
                                      r['allocated_quantity'] + r['unallocated_quantity'] == total_quantity
                                      for r in (full_result, split_result)),
            'conditions_used': {k: conditions[k] for k in ('effective_date', 'customer_due_date', 'production_daily_capacity',
                                'production_lead_days', 'production_delay_days', 'qa_days')},
            'capacity_basis': {k: full[k] for k in ('capacity_snapshot', 'daily_capacity_limit', 'residual_capacity_qty',
                                'capacity_period_assumption', 'working_days_assumption')},
            'alternatives': [full_result, split_result],
            'tradeoff': {'first_supply_earlier_calendar_days': (date.fromisoformat(full['earliest_ship_date']) - date.fromisoformat(first_ship)).days,
                         'final_supply_later_calendar_days': (date.fromisoformat(final_ship) - date.fromisoformat(full['earliest_ship_date'])).days,
                         'on_time_quantity_change': split_result['on_time_quantity'] - full_result['on_time_quantity']},
            'cost_comparison': {'status': 'unknown', 'amount': None, 'reason': '추가 검사·물류·전환 비용 근거가 없어 가격·절감액을 계산하지 않음'},
            'assumptions': full['assumptions'] + [
                '분할 공급은 같은 라인에서 선공급 생산 → 선공급 QA → 잔량 생산 → 잔량 QA 순서입니다. QA 중 생산 병행은 가정하지 않습니다.',
                '분할별 생산일은 각각 ceil(수량 / 일일 수량)입니다. 각 분할의 마지막 날 남는 능력을 다른 분할로 넘기지 않습니다.',
                '생산 준비·기존 지연은 처음 한 번, QA는 공급분마다 적용합니다. 분할 추가 셋업·배송 소요일은 미산정이므로 출하일은 고객 도착일이 아닙니다.',
                '생산 예약은 첫 생산일부터 잔량 생산 종료일까지 보수적으로 점유합니다. 예약 충돌을 자동으로 뒤로 재배치하지 않습니다.',
                '고객별 필요량·기한·배분은 모의 입력입니다. 0 또는 빈 값은 실제 재공급 불필요 확인이나 고객 합의가 아닙니다.'],
            'notice': '총량 보존 비교만 수행했습니다. 조건·의결·회신·예약·감사 이력은 바꾸지 않으며 기존 승인과 고객 합의를 승계하지 않습니다. 분할안 저장·생산·고객 연락은 실행하지 않습니다.'}
