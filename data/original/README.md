# Trusted CEO Agent Challenge — 데이터룸 (Aurora Holdings)

본 데이터룸은 Aurora Holdings와 자회사(Corevia Cosmetics 제조 · Masil Commerce 유통),
투자 후보(Celtonic Labs)의 30개월(2024-01 ~ 2026-06) 가상 데이터입니다.
모든 데이터는 시나리오 세계관에 기반한 **허구**이며, 실제 데이터가 아닙니다.

## 폴더 구조
- `00_common/` — 가장 먼저 확인 필요
- `01_corevia/` — Corevia Cosmetics (master·financials·procurement·production·quality·sales·docs)
- `02_masil/` — Masil Commerce (master·financials·inventory·marketing)
- `03_aurora_group/` — 그룹 자금요청·전략·승인
- `04_celtonic_labs/` — 투자 후보 실사 자료
- `05_external/` — 외부 출처(환율·성장률·벤치마크·밸류에이션·뉴스)

## 파일 형식 (고정)
- CSV: UTF-8, 헤더 1행, **파이프(`|`) 구분자**. 값 내부에 파이프 없음.
- 금액은 원(KRW) 정수·VAT 제외. 빈 셀 없음(해당 없음 = `NONE`).
- As-of / 컷오프: **2026-07-01**. 주차는 월요일 시작, 영업일 월~금.

먼저 `00_common/data_dictionary.md`와 `00_common/calculation_definitions.md`를 읽고 시작하세요.
