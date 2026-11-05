# Strategy-Pilot · Rule spec

Paper trading thuần: chỉ đọc data Deribit public API, không đặt lệnh thật. Tham số nằm ở `config.json`. Engine `pilot.py` thực thi các rule bắt buộc, còn Claude làm phần phán đoán.

## Tài khoản
- Vốn khởi điểm **10,000 USD**, tài khoản tính bằng USD.
- Trade trên sổ lệnh BTC options chính của Deribit (giá tính bằng BTC). Mỗi lần fill quy ra USD theo index tại thời điểm khớp. Không dùng BTC_USDC options vì thanh khoản quá mỏng.
- Fill: bán tại **best bid**, mua tại **best ask**, coi như lệnh market. Thiếu bid/ask thì dùng **mark**.
- Phí Deribit:
  - Mỗi lần khớp: 0.0003 BTC/contract, tối đa 12.5% giá option.
  - Khi expiry ITM: phí delivery 0.015% underlying, tối đa 12.5% giá trị option.
- Khối lượng làm tròn xuống theo bước 0.1 contract.

## Sizing
| ID | Rule |
|---|---|
| R-CAP | Tổng notional short **mỗi side ≤ 8 × equity**, cộng dồn mọi expiry. Muốn entry chu kỳ mới mà thiếu room thì đóng bớt lot của chu kỳ cũ, lot có ask thấp nhất (rẻ nhất để mua lại) đóng trước. |
| — | Margin: giả định portfolio margin. Ước tính standard margin chỉ ghi vào log (`std_margin_est_pct`), không chặn lệnh. |

## Entry
| ID | Rule |
|---|---|
| R-E1 | Bán Call và Put BTC, expiry là **thứ 6 tuần sau** (thứ 6 đầu tiên cách hôm nay ≥ 7 ngày). Mỗi expiry là một **chu kỳ (cycle)**. Ví dụ entry ngày 4–5/11 thì vào 13NOV26. |
| R-E1 | Khung entry (giờ VN): **thứ 4** 12h, 15h, 18h; **thứ 5** 09h, 12h, 15h. |
| R-E1 | Mỗi ngày entry tối đa **50% exposure cho phép** mỗi side, tức 4 × equity notional. Hai ngày cộng lại là 8x. |
| R-E1 | Chọn strike có \|delta\| trong khoảng **0.07–0.10**, ưu tiên gần 0.085 nhất. |
| R-E2 | Claude được **chọn khung** trong ngày, có thể vào Call và Put ở các khung khác nhau nếu thấy có lợi. Mỗi lệnh phải ghi rationale. |
| R-E3 | Đến **khung cuối của ngày** (thứ 4 18h, thứ 5 15h) mà quota ngày còn thì engine tự vào phần còn lại. |
| — | Side đã có Exit 2 trong cycle thì không entry thêm cho side đó trong cycle (chỉ còn roll). |

## Exit
| ID | Rule |
|---|---|
| R-X1 | Premium về **0.0001 BTC** (ask ≤ 0.0001, hoặc không có ask và mark ≤ 0.0001) thì mua lại để đóng. |
| R-X2 | Khoảng cách spot tới strike tính theo strike. **Call**: spot ≥ K × (1 − x). **Put**: spot ≤ K × (1 + x). Ngưỡng x: **3.5%** khi DTE < 7, **4%** khi 7 ≤ DTE < 8, **5%** khi DTE ≥ 8. Chỉ xét **spot tại lúc chạy**, fill theo giá tại lần chạy đó. Ví dụ Call 90k, DTE 6: spot ≥ 86.85k thì exit. |
| — | Expiry: settle theo delivery price của Deribit. |

## Roll
| ID | Rule |
|---|---|
| R-R1 | Sau **Exit 2** thì roll cùng side sang strike xa hơn có \|delta\| **0.14–0.16** (ưu tiên 0.15), dùng cùng expiry. Expiry +1 ngày (thứ 7) chỉ dùng khi đã được list. Khối lượng bằng khối lượng vừa exit. |
| R-R2 | Nếu nến 1H đã đóng gần nhất có (High − Low) / Low ≥ **2%** thì chưa roll. Các lần chạy sau kiểm tra lại. |
| R-R3 | Đang chờ roll mà DTE < **2** thì bỏ roll, vì momentum vẫn mạnh. |
| R-X2 | Roll leg cũng exit theo đúng R-X2. |
| R-SL | **Dừng** side đó trong cycle sau **2 lần Exit 2** (entry 1 → exit 1 → roll 1 → exit 2). |

## Phán đoán của Claude (ngoài rule)
- Claude được đề xuất làm khác rule, ví dụ đóng sớm, không roll, đổi strike, đổi size, hedge. Mỗi đề xuất là một record `suggestion` có ID `S-YYMMDD-HH-n`, kèm rationale và lệnh cụ thể. **Claude không tự thực hiện các đề xuất này.**
- Bạn duyệt bằng cách nhắn `approve S-…` ngay trong phiên chạy. Đề xuất không được duyệt sẽ hết hạn ở lần chạy kế tiếp.
- Riêng việc chọn khung entry (R-E2) là phán đoán được phép tự làm, không cần duyệt.

## Can thiệp trong phiên
Nhắn trong phiên của routine, ví dụ: `approve S-261111-12-1`, `pause call`, `resume put`, `đóng hết`, `đóng L-5`, `leverage 6`. Mọi can thiệp đều được ghi `user_cmd` hoặc `trade` với source `user`.
