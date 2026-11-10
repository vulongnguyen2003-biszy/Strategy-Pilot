# Strategy-Pilot · Mô tả chiến lược

Bản tóm tắt chiến lược, tổng hợp từ `RULES.md`, `config.json` và `pilot.py`. Khi có khác biệt thì `RULES.md` và `config.json` là chuẩn.

## 1. Ý tưởng
Chiến lược **bán short strangle OTM BTC options theo tuần** trên Deribit để thu premium (theta). Hai chân bán ở delta thấp (khoảng 0.085). Rủi ro được giới hạn theo hai cách:
- **Exit 2:** cắt sớm khi spot tiến gần strike, không chờ option vào tiền.
- **Roll** chân bị cắt ra strike xa hơn, tối đa 2 lần Exit 2 mỗi side mỗi chu kỳ.

Đây là **paper trading thuần**: chỉ đọc data public của Deribit, không đặt lệnh thật.

## 2. Tài khoản và giả định khớp lệnh
| Mục | Giá trị |
|---|---|
| Vốn khởi điểm | 10,000 USD, tài khoản tính bằng USD |
| Sản phẩm | BTC options inverse, sổ chính của Deribit, giá tính bằng BTC. Không dùng BTC_USDC vì quá mỏng |
| Quy đổi | Mỗi lần fill quy ra USD theo index tại thời điểm khớp |
| Fill | Bán tại best bid, mua tại best ask (như lệnh market). Thiếu bid/ask thì dùng mark |
| Phí khớp | 0.0003 BTC/contract, tối đa 12.5% giá option |
| Phí delivery (ITM lúc expiry) | 0.015% underlying, tối đa 12.5% giá trị option |
| Bước khối lượng | 0.1 contract, làm tròn xuống |
| Margin | Giả định portfolio margin. Ước tính standard margin (`std_margin_est_pct`) chỉ ghi log, không chặn lệnh |

## 3. Sizing
- **R-CAP:** tổng notional short **mỗi side ≤ 8 × equity**, cộng dồn mọi expiry.
- Mỗi ngày entry tối đa **50% của cap**, tức 4 × equity mỗi side. Hai ngày entry cộng lại đủ 8x.
- Khi cần room cho chu kỳ mới, engine đóng bớt lot của chu kỳ cũ, lot có **ask thấp nhất** (rẻ nhất để mua lại) đóng trước.

Ví dụ: equity 10,000 USD, BTC 75,000 thì cap mỗi side khoảng 80,000 USD, tức khoảng 1.0 contract. Mỗi ngày entry vào được khoảng 0.5 contract mỗi side.

## 4. Chu kỳ và entry
| ID | Rule |
|---|---|
| R-E1 | Expiry là **thứ 6 tuần sau**, tức thứ 6 đầu tiên cách hôm nay ≥ 7 ngày. Mỗi expiry là một chu kỳ (cycle) |
| R-E1 | Khung entry (giờ VN): **thứ 4** 12h, 15h, 18h; **thứ 5** 09h, 12h, 15h |
| R-E1 | Strike có \|delta\| trong **0.07–0.10**, ưu tiên gần 0.085 nhất |
| R-E2 | Claude tự chọn khung trong ngày, có thể vào call và put ở các khung khác nhau, có thể vào một phần quota. Mỗi lệnh phải ghi rationale |
| R-E3 | Đến khung cuối ngày (thứ 4 18h, thứ 5 15h) mà quota còn thì engine tự vào phần còn lại |
| — | Side đã có Exit 2 trong cycle thì không entry thêm cho side đó (chỉ còn roll) |

Khi chọn khung (R-E2), Claude xét bid, delta và IV của các strike ứng viên, momentum nến 1H, vị trí spot trong nhịp, và gap giữa call với put. Ví dụ spot vừa bật lên thì premium call tốt hơn, nên bán call trước và chờ put.

## 5. Exit
| ID | Điều kiện | Hành động |
|---|---|---|
| R-X1 | Premium về **0.0001 BTC** (ask ≤ 0.0001, hoặc không có ask và mark ≤ 0.0001) | Mua lại để đóng, chốt gần hết premium |
| R-X2 | Spot tiến gần strike. **Call:** spot ≥ K × (1 − x). **Put:** spot ≤ K × (1 + x) | Mua lại tại ask, rồi chuyển sang roll |
| — | Đến expiry | Settle theo delivery price của Deribit |

Ngưỡng x của R-X2 theo DTE:
| DTE | x |
|---|---|
| < 7 | 3.5% |
| 7 đến < 8 | 4% |
| ≥ 8 | 5% |

R-X2 chỉ xét spot (index) tại lúc chạy và fill theo giá của lần chạy đó. Ví dụ call 90k với DTE 6 sẽ exit khi spot ≥ 86,850.

## 6. Roll và stop
| ID | Rule |
|---|---|
| R-R1 | Sau Exit 2, bán lại cùng side ở strike xa hơn có \|delta\| **0.14–0.16** (ưu tiên 0.15). Expiry roll: thứ 6 của cycle, **+1 ngày (thứ 7)** hoặc **+2 ngày (chủ nhật)**, chỉ dùng expiry đã list và còn DTE ≥ 2. Khối lượng bằng khối lượng vừa exit |
| R-R2 | Nếu nến 1H đã đóng gần nhất có (High − Low) / Low ≥ **2%** thì chưa roll. Các lần chạy sau kiểm tra lại |
| R-R3 | Đang chờ roll mà **cả ba** expiry roll (T6, T7, CN) đều có DTE < **2** thì bỏ roll |
| R-X2 | Lot roll cũng exit theo R-X2 |
| R-SL | Sau **2 lần Exit 2** trong một cycle (entry → exit 1 → roll → exit 2), dừng side đó cho cycle |

Roll ở delta 0.15 sau khi đã bị đẩy ra là cách lấy lại premium đã mất, với strike vẫn nằm xa hơn strike cũ. Hai bộ lọc R-R2 và R-R3 để tránh roll khi momentum còn mạnh.

### Chi tiết R-R1 và R-R3: chọn expiry roll, khi nào bỏ roll
**Expiry được phép roll.** Với cycle có expiry thứ 6 (ví dụ 13NOV26), lot roll được đặt vào một trong ba expiry:

| Expiry | Ví dụ | Đáo hạn (giờ VN) | Còn DTE ≥ 2 tới |
|---|---|---|---|
| Thứ 6 (cùng expiry) | 13NOV26 | T6 15:00 | T4 15:00 |
| +1 ngày (thứ 7) | 14NOV26 | T7 15:00 | T5 15:00 |
| +2 ngày (chủ nhật) | 15NOV26 | CN 15:00 | T6 15:00 |

Deribit đáo hạn lúc 08:00 UTC, tức 15:00 VN. Option daily được list khoảng 4 ngày trước đáo hạn: expiry thứ 7 có từ khoảng thứ 3, chủ nhật từ khoảng thứ 4. Vì vậy khi expiry thứ 6 tụt dưới DTE 2 thì expiry thứ 7 và chủ nhật thường đã có sẵn. Engine không roll về expiry sớm hơn expiry của lot vừa exit.

**Cách engine xử lý.** Sau mỗi Exit 2 chưa chạm R-SL, engine tạo một **roll chờ** (`pending_roll`) cho cặp cycle/side. Roll chờ ghi lại khối lượng vừa exit, strike cũ và expiry. Mỗi lần chạy, engine xử lý roll chờ theo thứ tự:
1. Side đang bị `pause` thì bỏ qua lần chạy này, roll chờ vẫn giữ nguyên.
2. **Lọc expiry theo DTE:** chỉ giữ các expiry (T6, T7, CN) còn DTE ≥ 2.
   - Không còn expiry nào thì xóa roll chờ, flag `roll_cancelled` (R-R3).
   - Còn expiry hợp lệ nhưng chưa list thì flag `cycle_not_listed` và chờ lần chạy sau.
3. **R-R2:** nến 1H gần nhất có range ≥ 2% thì chưa roll.
4. **Chọn expiry:** duyệt từ gần đến xa, lấy expiry đầu tiên có strike delta 0.14–0.16 xa hơn strike cũ. Không expiry nào có strike trong band thì lấy strike gần band nhất của expiry gần nhất và flag `delta_out_of_band`.
5. Mở lot roll. Lot roll **vẫn thuộc cycle cũ**, nên vẫn tính vào R-SL (2 lần Exit 2) và R-CAP như lot thường. Ngưỡng Exit 2 của lot roll tính theo DTE của chính expiry mới.

**Hệ quả theo thời điểm Exit 2** (cycle 13NOV26):

| Exit 2 lúc (giờ VN) | Expiry roll |
|---|---|
| Trước T4 15:00 | Roll vào 13NOV26 (thứ 6) nếu có strike trong band |
| T4 15:00 → T5 15:00 | Roll vào 14NOV26 (thứ 7) |
| T5 15:00 → T6 15:00 | Roll vào 15NOV26 (chủ nhật) |
| Sau T6 15:00 | Cả ba expiry đều DTE < 2, bỏ roll |

Roll chờ bị R-R2 chặn nhiều lần thì có thể dịch dần sang expiry xa hơn, cho tới khi cả ba đều hết DTE.

**Sau khi bỏ roll:**
- Side đó không còn vị thế nào trong cycle, và cũng không được entry thêm vì đã có Exit 2.
- `exit2_count` không tăng và side không bị đánh dấu `stopped`.
- Cycle tuần sau vẫn entry bình thường.

**Lý do giữ ngưỡng DTE ≥ 2 cho lot roll:**
- **Premium.** Dưới 2 ngày thì option delta 0.15 có giá trị thời gian nhỏ, phí khớp và spread ăn gần hết.
- **Gamma.** Sát expiry, delta đổi rất nhanh theo spot, trong khi các lần chạy cách nhau khoảng 2 giờ.
- **Momentum.** Roll ngay sau một nhịp mạnh dễ dính Exit 2 lần nữa (R-SL).

Expiry thứ 7 và chủ nhật cho lot roll thêm 1–2 ngày thời gian, nên roll vẫn còn premium khi expiry thứ 6 đã quá gần.

**Rủi ro của roll sang cuối tuần:**
- Option daily cuối tuần thường **mỏng thanh khoản hơn**, spread rộng hơn.
- Lot roll **chồng thời gian với cycle tuần sau** trên cùng side. Tổng notional vẫn bị chặn bởi R-CAP. Khi thiếu room, `make_room` chỉ đóng lot của cycle khác, nên lot roll của cycle cũ có thể bị đóng sớm để nhường room cho entry cycle mới.
- Thị trường cuối tuần có thể gap. Engine vẫn chỉ kiểm tra Exit 2 theo lịch chạy (khoảng 2 giờ một lần).

**Ví dụ.** Cycle 13NOV26, put 70000:
- Thứ 4 11/11, 16:10 VN, spot 72,400 ≤ 70,000 × 1.035 = 72,450 → Exit 2. Expiry 13NOV26 còn DTE 1.95 nên bị loại. 14NOV26 còn DTE 2.95. Nến 1H vừa đóng có range 1.2% nên không bị R-R2 chặn.
- Engine bán put 14NOV26 strike xa hơn 70000, delta khoảng 0.15. Lot này đáo hạn T7 15:00 VN.
- Khung 18h thứ 4 vẫn entry put cho cycle 20NOV26 như thường, chung R-CAP với lot roll.

## 7. Vòng đời một tuần
```
Thứ 4–5   entry chu kỳ tuần sau (2 × 50% cap mỗi side)
   │
   ├─ premium ≤ 0.0001        → R-X1 đóng, chốt lãi
   ├─ spot chạm ngưỡng R-X2   → đóng → (chờ nến 1H êm) → roll delta 0.15
   │        └─ roll bị R-X2 lần nữa → đóng, dừng side (R-SL)
   └─ thứ 6 expiry            → settle theo delivery price
```
Hai chu kỳ có thể chồng nhau, vì thứ 4–5 tuần này đã vào chu kỳ tuần sau trong khi chu kỳ tuần này chưa đáo hạn. R-CAP tính chung cả hai.

## 8. Vận hành
- Cloud routine chạy **14 lần/ngày**: mỗi 2 giờ, thêm 09:10 và 15:10 giờ VN.
- Mỗi lần chạy gọi `python pilot.py run`. Engine tự làm theo thứ tự: exit (R-X1, R-X2, expiry) → roll → entry khung cuối (R-E3) → flag → ghi obs. Sau đó Claude quyết định entry trong khung (R-E2), commit lên `main` và gửi báo cáo.
- Flag cảnh báo:
  - `near_exit2`: spot cách ngưỡng Exit 2 dưới 1%
  - `spot_move`: spot đổi ≥ 3% so với lần chạy trước
  - Các flag về roll: chờ, bỏ, dừng
- Mọi thay đổi trạng thái đi qua `pilot.py`. Log append-only trong `logs/trade-log.jsonl`, trạng thái nằm trong `state.json`.

## 9. Phán đoán và can thiệp
- **Claude tự làm được:** chọn khung và size entry trong quota (R-E2).
- **Claude chỉ đề xuất:** đóng sớm, bỏ roll, đổi strike hoặc size, hedge, giảm exposure trước sự kiện macro. Tối đa 2 đề xuất mỗi lần chạy, mỗi đề xuất có ID `S-YYMMDD-HH-n`. Đề xuất hết hạn ở lần chạy kế tiếp nếu chưa được duyệt.
- **Trader can thiệp trong phiên:**
  - `approve S-…`
  - `pause call` / `resume put`
  - `đóng L-n` / `đóng hết`
  - Đổi tham số, ví dụ `leverage 6`

## 10. Hồ sơ rủi ro
- **Lợi nhuận:** thu theta của option delta thấp có DTE khoảng 8–10 ngày lúc vào. Phần lớn lot kỳ vọng kết thúc qua R-X1 hoặc expiry OTM.
- **Rủi ro chính:** BTC chạy mạnh một chiều. Leverage notional 8x mỗi side khá cao. Exit 2 giới hạn lỗ, nhưng vì chỉ kiểm tra theo lịch chạy (khoảng 2 giờ một lần) nên vẫn có gap risk giữa hai lần chạy. Lỗ cũng phình khi IV tăng vọt đúng lúc phải mua lại.
- **Giới hạn đuôi:** R-SL chặn tối đa 2 lần cắt lỗ mỗi side mỗi cycle. R-R2 và R-R3 tránh roll ngay trong nhịp biến động mạnh.
