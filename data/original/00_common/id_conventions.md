# id_conventions — 전 엔티티 ID 체계

| 엔티티 | 규칙 | 예시 |
|---|---|---|
| 제품군(Corevia) | PL-0n | PL-01 기초 ~ PL-06 바디 |
| 원재료 | M-x | M-A ~ M-F |
| 공급처 | V-2nn | V-201, V-205(더마 독점) |
| B2B 고객(Corevia) | C-1nn | C-101, C-102 |
| 신규 입찰 | C-2nn | C-201 RFP |
| 대리점/거래처(Corevia) | D-nnn | D-001 ~ D-050 |
| 공장/라인 | F1·F2 / L-nn | F1(국내)/F2(해외) |
| 배치·로트 | batch_id / lot_id | B-*, LOT-* |
| SKU(Masil) | SKU-nnnn | SKU-1001 등 |
| 유통 파트너(Masil) | LC-nnnn | LC-0001(최대 고객) |
| 채널(Masil) | CH-nn | CH-01 자사몰 등 |
| 캠페인 / 인플루언서 | CMP-nnnn / INF-nnn | CMP-0001 / INF-001 |
| 고객(Celtonic) | CL-nn | CL-01(1위·매출 38%) ~ CL-10. 실사 초기라 실명 비공개 |

- 코드/FK 컬럼의 "해당 없음"은 `NONE`으로 표기한다.
