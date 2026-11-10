# Strategy-Pilot · Reverse engineering chiến lược sell BTC options

Ngày viết: 10/11/2026. Mục tiêu: dựng lại chiến lược từ **hành vi thật** (track log, lịch sử commit, các quyết định của trader, prompt routine) thay vì chỉ chép lại rule. Ở mỗi mục có ghi rõ cái nào là rule đã viết, cái nào là suy luận.

## 0. Nguồn đã đọc
| Nguồn | Nội dung dùng được |
|---|---|
| `logs/trade-log.jsonl` (70 record) | 67 obs, 2 trade, 1 flag. Phủ từ 05/11 23:29 đến 10/11 15:13 VN |
| Lịch sử git (73 commit, cả nhánh đã bị force-push) | 6 commit không phải routine, lộ ra các lần trader đổi rule |
| `RULES.md`, `config.json`, `pilot.py`, `STRATEGY.md`, `ROUTINE.md` | Rule hiện hành và bản gốc (commit `eb0a0ea`) |
| Prompt routine trong phiên này | Bản mới hơn `ROUTINE.md`: bắt buộc báo cáo mọi lần check |
| GitHub issue và PR | Không có (0 issue, 0 PR) |

Giới hạn: tôi không đọc được transcript các phiên chat khác (ví dụ phiên đổi rule roll ngày 10/11). Phần "trader muốn gì" được suy ra từ commit message, field `reason` trong log, và các thay đổi rule.

---

## 1. Chiến lược trong một đoạn
**Bán short strangle BTC weekly trên Deribit**, chân delta ~0.085, vào lúc còn 8–10 ngày tới expiry. Notional rất lớn, ~8× equity mỗi side, nên bản chất là **thu theta có đòn bẩy, sống nhờ portfolio margin**. Rủi ro không được quản bằng size hay hedge. Nó được quản bằng **stop theo khoảng cách spot tới strike** (Exit 2), rồi **roll** chân bị cắt ra delta 0.15 để thu lại premium, và **dừng hẳn sau 2 lần cắt**. Edge mà trader đặt cược là variance risk premium của BTC: IV (~40–47 lúc vào) thường cao hơn biến động thực tế trong một tuần.

---

## 2. Cấu trúc đã dựng lại

### 2.1 Sản phẩm và tenor
- BTC inverse options sổ chính của Deribit. Loại BTC_USDC vì mỏng, cho thấy trader **ưu tiên chất lượng khớp hơn việc khử rủi ro tỷ giá BTC/USD**.
- Expiry là **thứ 6 tuần sau**, vào lệnh thứ 4–5, nên DTE lúc vào khoảng 8–9.5 ngày.
- **Suy luận:** chọn khoảng 8–9 DTE để nằm ở đoạn theta/ngày đã cao nhưng gamma chưa quá dốc như option 1–3 ngày. Hai cycle chồng nhau khoảng 2 ngày mỗi tuần, nên vốn luôn được dùng.

### 2.2 Chọn strike
- |Δ| 0.07–0.10, nhắm 0.085. Với IV khoảng 43% và 8 ngày, mức này tương đương strike cách spot khoảng **8–10%**. Ví dụ thực tế: call 83k (+9.3%) và put 70k (−7.8%) khi spot 75.9k.
- **Suy luận:** trader chọn delta theo xác suất (~8.5% chạm ITM mỗi chân) chứ không theo khoảng cách cố định, nên strike tự giãn khi IV cao.
- Skew lộ ra trong log: put 70k có IV 46.8 so với call 83k IV 43.4, và premium put cao hơn 43% (0.0033 so với 0.0023 BTC). Phần lớn premium đến từ **chân put**.

### 2.3 Sizing: đòn bẩy notional là biến số chính
- R-CAP 8× equity mỗi side, chia 2 ngày, mỗi ngày 4×. Thực tế là 1.0 contract mỗi side, ~7.3–7.75×.
- `std_margin_est_pct` dao động 146–162%. **Dưới standard margin, tài khoản này đã bị margin call ngay từ lúc vào.** Chiến lược chỉ tồn tại được dưới portfolio margin. Đây là giả định ngầm quan trọng nhất.
- Premium thu được mỗi tuần khoảng 4.25% equity gộp, sau phí còn khoảng 3.8%. Nếu tuần nào cũng như vậy thì lợi nhuận danh nghĩa trên 100%/năm. Con số này là phần thưởng cho rủi ro đuôi rất dày (mục 4).

### 2.4 Timing entry
- Khung T4 12/15/18h, T5 09/12/15h giờ VN, tức phiên Âu và đầu phiên Mỹ. Claude chọn khung (R-E2), còn khung cuối thì engine tự vào phần quota còn lại (R-E3).
- **Hành vi thật thì khác rule:** cycle đầu tiên, trader ra lệnh *"Hãy entry 100% ngay bây giờ"* lúc 00:26 thứ 6 (ngoài khung, bỏ qua giới hạn 50%/ngày). Điều này cho thấy timing entry là **tối ưu bậc hai** với trader: được vào thị trường quan trọng hơn canh giờ.

### 2.5 Exit: stop theo khoảng cách, không theo giá
| Lớp | Cơ chế | Ý nghĩa ngầm |
|---|---|---|
| R-X1 | Mua lại khi ask ≤ 0.0001 BTC | Không ôm tail risk vài USD cuối, giải phóng room cho cycle mới |
| R-X2 | Spot tới cách strike x% (5% → 4% → 3.5% theo DTE) | Stop **theo vị trí spot**, không theo bội số premium (kiểu 2× hay 3× credit) |
| Expiry | Settle theo delivery | Đường mặc định nếu không chạm hai lớp trên |

**Suy luận về R-X2:**
- Trader không tin stop theo giá option vì IV giật làm giá option phình dù spot chưa đi xa. Bằng chứng trong log: 06/11 06:10, spot chỉ giảm 1.1% mà put IV nhảy 46.8 → 55.5, PnL chưa chốt của put là −$305 (≈1.2× premium). Nếu stop theo bội số premium thì lệnh đã bị cắt ở đây, trước khi phục hồi về +$176 hiện tại.
- Ngưỡng siết dần theo DTE (5% → 3.5%) bù cho gamma tăng khi gần expiry.
- Lần nới **3% → 3.5%** (commit `fea8bb8`, 8 phút sau khi vào lệnh) cho thấy trader thấy 3% quá sát: với gamma của option 1–6 ngày, chờ tới 3% thì lỗ mua lại đã quá lớn. Kết quả là **chấp nhận cắt sớm hơn** để đổi lấy lỗ nhỏ hơn mỗi lần.

### 2.6 Roll và stop side
- Sau Exit 2, bán lại cùng side ở |Δ| 0.15, cùng khối lượng, strike xa hơn strike cũ. Delta gần gấp đôi lúc vào, vì phải có premium đủ để **bù lỗ của lần Exit 2**.
- Bộ lọc **R-R2**: nến 1H có range ≥ 2% thì chưa roll, tránh bán ngay giữa nhịp mạnh.
- **R-SL**: tối đa 2 lần Exit 2 mỗi side mỗi cycle. Lỗ tối đa mỗi side mỗi tuần khoảng (lỗ exit 1 + lỗ exit 2) trừ premium đã thu.
- Lần đổi ngày 10/11 (`e9cdf89`): roll được sang expiry **thứ 7 và chủ nhật**, chỉ bỏ roll khi cả ba expiry còn dưới 2 DTE. **Suy luận:** trader không muốn mất cơ hội thu hồi lỗ chỉ vì Exit 2 rơi vào cuối tuần, kể cả khi phải chấp nhận thanh khoản mỏng hơn ở option daily cuối tuần.

### 2.7 Lớp phán đoán (Claude) và quyền của trader
- Claude chỉ tự quyết **khi nào** vào trong khung, còn mọi chệch rule khác (đóng sớm, bỏ roll, đổi size, hedge) đều là suggestion chờ approve.
- Trader giữ quyền override tuyệt đối (`trade --source user`, `set`). Trong thực tế đã dùng 3 lần: entry 100% ngoài khung, nới Exit 2, mở rộng expiry roll.
- Prompt routine đã đổi từ "chỉ báo khi có event" (`ROUTINE.md`) sang "**báo cáo mọi lần check**". Trader muốn theo dõi liên tục, không chỉ nhận cảnh báo.

---

## 3. Track record cycle 13NOV26 (cycle duy nhất tới nay)
| Chỉ số | Giá trị |
|---|---|
| Entry | 06/11 00:26, spot 75,945. L-1 83000-C @0.0023 (Δ0.087, IV 43.4). L-2 70000-P @0.0033 (Δ0.103, IV 46.8, **ngoài band** vì không có strike trong band) |
| Premium gộp / phí | $425.30 / $44.62 (phí ≈ 10.5% premium) |
| Equity hiện tại | **$10,291 (+2.9%)**, PnL chưa chốt +$336 |
| Max drawdown | **−3.1%** ($9,690) lúc 06/11 06:10, khi spot chỉ −1.1%. Nguyên nhân chính là IV put tăng vọt (vega), không phải spot |
| Spot range | 73,826 – 76,132 (biên độ ~3%, khá êm) |
| Lần nguy hiểm nhất | 09/11 15:15: spot 73,826, put Δ0.16, cách ngưỡng Exit 2 (72,450) **1.9%** |
| Exit / roll | Chưa có. Không có Exit 2, chưa chạm R-X1 |
| Hiện trạng (DTE 3) | Call mark 0.0002 (sắp chạm R-X1), put mark 0.0010 |

**Đọc từ track record:**
1. Tuần êm (DVOL 39–43, spot ±3%) thì chiến lược làm đúng thiết kế: theta trả ~4%/tuần.
2. Rủi ro đến **sớm hơn và qua vega nhiều hơn** người ta tưởng. Drawdown sâu nhất xảy ra ngay 6 giờ sau khi vào, lúc spot còn cách ngưỡng Exit 2 tới 3.2%.
3. Chân put là nơi tập trung cả lợi nhuận (59% premium) lẫn rủi ro (cả hai lần căng thẳng đều ở put).

---

## 4. Hồ sơ payoff đã suy ra
- **Phân phối:** thắng nhỏ, đều, khoảng +3–4%/tuần, kèm đuôi trái dày.
- **Lỗ mỗi lần Exit 2 (ước tính):** khi spot chạm ngưỡng thì option thường có Δ khoảng 0.30–0.40 và IV đã lên, nên giá mua lại khoảng 3–5× premium. Với 1 contract và 8× đòn bẩy, mỗi lần mất khoảng **−5% đến −10% equity**. Kịch bản xấu nhất mỗi side mỗi cycle (2 lần Exit 2, roll thua) là khoảng **−10% đến −20%**.
- **Breakeven ngầm:** khoảng 1 lần Exit 2 mỗi 2–3 tuần là hòa vốn. Edge phụ thuộc vào việc BTC hiếm khi đi được 5–7% trong một tuần theo một chiều.
- **Các rủi ro không được rule bảo vệ:**
  - Gap giữa hai lần chạy (~2 giờ).
  - **Sàn ngừng hoạt động.** Lần chạy 10/11 ~17:10 VN đã fail vì Deribit `system_maintenance`. Trong lúc đó không có exit nào chạy được.
  - Thanh khoản mỏng khi phải mua lại giữa nhịp.
  - Margin thật dưới standard margin.

---

## 5. Lệch giữa rule và thực tế / điểm cần làm rõ
| # | Quan sát | Hệ quả |
|---|---|---|
| 1 | Entry 100% ngoài khung, bỏ quota 50%/ngày | R-E1 đến R-E3 chưa được kiểm chứng lần nào |
| 2 | Put vào ở Δ0.103, ngoài band 0.07–0.10 | Engine fallback sang strike gần nhất thay vì chờ. Cần quyết định là chấp nhận hay chặn |
| 3 | `ROUTINE.md` cũ hơn prompt đang chạy (bước 5) | Tài liệu lệch với vận hành |
| 4 | Std margin 146–162% | Nếu áp dụng thật trên tài khoản không có PM thì không vào được size này |
| 5 | Phí khoảng 10% premium | Ở delta thấp, phí 0.0003 BTC/contract ăn đáng kể. Đi xa hơn Δ0.07 gần như không đáng |
| 6 | Không có rule cho downtime của sàn hay data | Nên có cách xử lý khi Exit 2 bị lỡ (ví dụ check ngay ở lần chạy kế tiếp, hoặc flag "missed window") |
| 7 | Chưa có rule về sự kiện macro (FOMC, CPI) | Hiện chỉ là suggestion tùy ý của Claude |

## 6. Câu hỏi để chốt chiến lược
1. Entry 100% một lần có phải cách làm chuẩn không, hay chỉ là ngoại lệ cho cycle đầu? Nếu là chuẩn thì nên bỏ chia 2 ngày.
2. Khi không có strike trong band delta thì vào ở strike gần nhất hay bỏ qua side đó?
3. Có muốn thêm stop vega (ví dụ DVOL +X điểm) bên cạnh stop theo khoảng cách không?
4. Đòn bẩy 8× được chọn theo tiêu chí gì: mục tiêu lợi nhuận/tuần, hay mức drawdown chịu được khi dính 2 lần Exit 2?
5. Có muốn giảm size hoặc bỏ qua cycle trước các sự kiện macro lớn không?
