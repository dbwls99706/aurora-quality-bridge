# calculation_definitions — 필수 계산 정의

> 채점 대상 지표의 **고정 계산식**입니다. 모든 팀은 아래 정의를 그대로 사용합니다.
> 회사명은 Corevia Cosmetics(제조) / Masil Commerce(유통) 기준입니다.

## 1. 필수 계산 정의

### 1.1 Corevia 마진 분해
```
Gross Margin = Gross Profit / Revenue

Material Cost Impact =
    Current Applied Material Cost - Baseline Applied Material Cost

FX Impact =
    Current Foreign Currency Unit Price × (Current FX - Baseline FX)

Unit Price Impact =
    (Current Foreign Currency Unit Price - Baseline Foreign Currency Unit Price)
    × Baseline FX
```
- **기준**: 구매단가가 아니라 `production_material_consumption_cv.applied_unit_cost_krw`
  (실제 투입 시점 기준 적용 단가) 기준으로 분석한다.
- Foreign Currency Unit Price = `procurement_material_purchase_cv.unit_price_fx`,
  Current/Baseline FX = `ext_fx_daily.csv`의 해당일 종가.
- Baseline 기준월 = 2024-07(환율 1,320 기준).
- **교차항 귀속**: 위 정의는 단가·환율의 교차항(Δ단가 × Δ환율)을 **환율효과에 귀속**시킨다.
  이 규칙을 따르면 `FX Impact + Unit Price Impact ≈ 총 원화단가 변동`이 성립한다.
- **잔차 주의**: `applied_unit_cost_krw`는 로트 투입 시차 때문에 **해당월 실효환율**을 반영하므로,
  위 Baseline FX(1,320 = 2024-07-01 종가)를 그대로 대입하면 **총 변동의 1% 미만 잔차**가 남는다.
  잔차를 0으로 맞추려면 `applied_unit_cost ÷ unit_price_fx`로 역산한 **실효환율**을 쓴다.
  **어느 쪽으로 계산하든 기여도 대소 판단은 바뀌지 않으므로 둘 다 인정한다.**
- **전가 여부 확인(필수)**: 원가 상승분을 판가로 전가할 수 있는지는 고객별로 다르다.
  `m_customer_cv.cost_linked_clause`(원가연동 조항 보유 여부)를 반드시 함께 조회하여
  **고객별로** 전가 가능/불가를 구분한다. 전사 평균만 보면 "판가를 올리면 된다"는 오답에 이른다.
- 마진 하락의 원인 귀속은 **제품군(PL-0n)별 × 원료(M-x)별**로 분해한다.
  원료의 조달 통화(`m_material_cv.currency`)에 따라 환율 노출이 다르므로 전사 합산은 원인을 가린다.

### 1.2 Masil 현금 시뮬레이션
```
Closing Cash[t]
  = Opening Cash[t]
  + Expected Collections[t]
  - Expected Payments[t]
  - Other Cash Outflows[t]

Covenant breach =
    closing_balance < covenant_min_krw
```
- `covenant_min_krw`(30억)를 **매 영업일 1회라도** 하회하면 covenant 위반(기한이익상실).
  기간 평균이 아니다.
- 가용현금 = `closing_balance`만 포함(크레딧라인·MMF 제외).
- **실적 구간(~as-of 2026-07-01)**: `fin_cash_daily_ms.csv`의 `closing_balance`를 그대로 사용한다.
  이 구간은 `closing = opening + inflow − outflow` 항등식이 성립하므로 재계산으로 검증 가능하다.
- **전망 구간(as-of 이후)**: 판단 대상은 과거가 아니라 **향후 90일**이다. 자금일보에는 미래가 없으므로
  아래 스케줄을 **직접 합산해 일별/주별로 재시뮬레이션**해야 한다. 이 표들을 쓰지 않으면 하회 시점을
  찾을 수 없다.

  | 항 | 산출 소스 |
  |---|---|
  | Opening Cash | `fin_cash_daily_ms.csv`의 as-of 종가 |
  | Expected Collections | `fin_ar_aging_ms.csv`의 `outstanding_krw`를 `due_date`에 배치 |
  | Expected Payments | `fin_ap_aging_ms.csv` 중 **`po_id = 'NONE'`인 행**의 `amount_krw`를 `payment_due_date`에 배치 |
  | Other Cash Outflows | `sc_purchase_orders_ms.csv`의 `amount_krw`를 `payment_due_date`에 배치 |

- **이중계상 금지(중요)**: `fin_ap_aging_ms.csv`에서 `po_id`가 `sc_purchase_orders_ms.csv`에 존재하는 행은
  재고 발주 대금을 매입채무로 다시 기재한 것이다. Expected Payments와 Other Cash Outflows에
  **양쪽 모두 더하면 발주 대금이 두 번 빠져** 잔액이 음수가 된다.
  → Expected Payments는 **`po_id='NONE'`(운영 매입채무)만** 집계한다.

- **주의**: `fin_cash_forecast_ms.csv`는 재무팀이 작성한 **공식 전망**일 뿐 정답이 아니다.
  작성 시점의 가정이 현재 조건과 다를 수 있으므로, 위 스케줄로 산출한 값과 **반드시 대조**하고
  차이가 나면 그 원인을 설명해야 한다.
- LC-0001 등 대형 파트너의 결제조건 변경은 `lc0001_payment_term_notice.md`와
  `covenant_terms_ms.md`를 함께 확인한다. 회수 시점 가정이 바뀌면 하회 시점도 바뀐다.

### 1.3 재고 커버
```
Available Qty  = on_hand_qty - reserved_qty
Weeks of Supply =
    available_qty / trailing_4w_avg_weekly_demand
Suggested Order Qty =
    max(Target Qty - available_qty - in_transit_qty, 0)
Final Order Qty =
    MOQ 제약 반영 후 산정
```
- trailing_4w_avg_weekly_demand = `sku_weekly_sales_ms.csv` 최근 4주 평균.
- 커버 계산의 현재고는 `on_hand_qty`만 포함(`in_transit_qty` 미합산).
- 본 데이터룸에는 별도 예약수량(reserved_qty)이 없으므로 Available Qty = on_hand_qty.

### 1.4 캠페인 실질 ROAS
```
Nominal ROAS = Attributed GMV / Marketing Spend
Real ROAS    = Contribution Profit / Marketing Spend

Contribution Profit =
    Net Revenue
    - COGS
    - Coupon Discount
    - Influencer Fee
    - Paid Media Spend
    - Channel Fee
    - Fulfillment Cost
    - Return Refund
    - Return Logistics Cost
```
- Net Revenue = GMV − 반품환불액 (GMV는 쿠폰 반영 후·VAT 제외).
- **Marketing Spend = `mkt_ad_spend_ms.csv`의 `ad_cost_krw` 전액**
  (media = `인플루언서` + `퍼포먼스` + `오프라인`). 캠페인에 귀속된 마케팅 지출을 모두 포함한다.
  위 Contribution Profit 산식의 `Influencer Fee`·`Paid Media Spend`는 이 지출의 주요 항목을 예시한 것이다.
- COGS·Channel Fee·물류비 = `mkt_cost_master_ms.csv` 고정값. 캠페인 귀속 = last-click 7일.
- 위 산식을 **정의 그대로** 적용해 산출한 값을 공식값으로 한다.

### 1.5 Celtonic Labs 정상화 EBITDA
```
Normalized EBITDA
  = EBITDA
  + 인정 가능한 비경상 조정
```
- 3년 연속 반복되는 "일회성" 항목은 경상 비용 가능성이 있으므로 별도 검토한다.
  전액 부인 또는 전액 인정이 아니라 **항목별 판단**이 필요하다.
- `strat_celtonic_adjustment_analysis.csv`의 `recurring_flag`·`normalized_ebitda_krw` 참조.

### 1.6 재구매율 (Repurchase Rate)
```
Repurchase Rate = 90일내 재구매 고객수 / 최초구매 고객수
```
- 재구매 = 최초주문 후 **90일 이내 재주문**(`mkt_repurchase_ms.csv.reorder_within_90d = True`).
- 분모 = 해당 기간 최초구매 고객수(고유 `customer_ref`). 벤치마크: `ext_industry_benchmark`(22%).

### 1.7 반품률 (Return Rate)
```
Return Rate = Σ 반품환불액 / Σ GMV
```
- **금액 기준**(건수 아님). 반품은 **승인일**(`refund_completed_date`) 기준 집계.
- 원 주문 기준 분석이 필요하면 `original_order_date` 사용. 벤치마크: 8%.

### 1.8 DSO (매출채권 회수기간)
```
DSO(일) = 기말 미수금(Σ outstanding_krw) / 기간 매출 × 기간일수
```
- 미수금 = AR aging의 `outstanding_krw`. 기간 매출 = 출하 기준 매출(Corevia) 또는 Net Revenue(Masil).
- 벤치마크: 55일. 이상치 대조용 기준선.

### 1.9 배치 불량률·회수 판정
```
Batch Defect Rate = 규격이탈확정 수량 / 배치 수량
회수 대상 = (defect_confirmed = True) AND (Batch Defect Rate ≥ 10%)
```
- 분자 = `qc_claims_cv.defect_confirmed = True` 건의 `qty_affected` 합(배치 단위).
- 분모 = 해당 `batch_id`의 생산·출하 수량(`prod_batch_log_cv` / `shipment_batch_link_cv`).
- **예비검사 미확정(보류/False) 건은 회수 대상이 아니라 조사·소명 대상**. 회수 판단 단위 = 배치.

### 1.10 매체(SNS) 효율 — CTR · CVR · CAC
```
CTR = clicks / impressions
CVR = conversions / clicks
CAC = 퍼포먼스 광고비 / conversions
```
- `mkt_traffic_inflow_ms.csv`의 `platform`(Instagram/YouTube/TikTok)·`sns_format`(피드·릴스·롱폼·숏츠·틱톡)·
  `impressions`·`clicks`·`conversions`로 산출. 광고비 = `mkt_ad_spend_ms.csv`의 `media='퍼포먼스'` 지출을 `sns_format`별 집계.
- **노출·클릭(CTR)이 높아도 CVR이 낮으면 CAC가 높다** — 명목 지표(노출·클릭)와 실질 효율(CAC)을 분리해서 본다.
