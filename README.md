# MeetNote

Ứng dụng Flask tạo transcript, biên bản Markdown và báo cáo Word từ một hoặc
nhiều file audio. Mặc định lịch sử cuộc họp được lưu trong Microsoft SQL Server;
có thể dùng `--no-database` cho demo tạm thời không cần database.

## Chạy không có database

```powershell
python app.py --no-database
```

Chế độ này không kiểm tra, đọc hoặc ghi SQL Server. Sidebar, xem kết quả, sửa
Markdown và tải DOCX vẫn hoạt động, nhưng dữ liệu chỉ nằm trong RAM và mất khi
tiến trình Flask dừng. Có thể kết hợp với Gemini:

```powershell
python app.py --no-database --api
```

Nếu không có `--api`, bước tóm tắt dùng model Ollama được cấu hình trong
`app.py`.

## Quy trình duyệt transcript trước khi tóm tắt

MeetNote không còn gọi LLM ngay sau khi phiên âm. Luồng xử lý hiện tại là:

1. Tạo transcript và, nếu được bật, chạy speaker diarization.
2. Mở transcript trong Markdown editor để người dùng kiểm tra.
3. Hiển thị timeline các lượt nói và cho phép đổi tên một speaker trên toàn bộ
   transcript, ví dụ `người nói 1` thành `Giám đốc An`.
4. Chỉ khi người dùng bấm **Tóm tắt transcript**, nội dung đã chỉnh sửa mới được
   gửi tới Ollama hoặc Gemini.

## Demo một ngày trên Google Colab với Ollama

Chọn `Runtime → Change runtime type → T4 GPU`, sau đó chạy lần lượt các cell.
Không cần cài SQL Server, SSMS hay Microsoft ODBC Driver khi dùng
`--no-database`.

Không sửa `app.py` trong Colab. Trong toàn bộ hướng dẫn, chỉ cần thay URL GitHub
ở bước 1. Cờ `--no-database` quyết định app bỏ qua SQL Server; việc có dùng
Gemini hay không được quyết định bằng cờ `--api`.

Thứ tự bắt buộc là:

1. Clone repo và cài dependency.
2. Khởi động Ollama và tải model.
3. **Chạy `app.py` bằng cell khởi động Flask.**
4. Xác nhận Flask trả HTTP 200.
5. Khởi động Cloudflare Tunnel và mở URL được cấp.

### 1. Clone và cài dependency

```python
!git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
%cd YOUR_REPOSITORY
!apt-get update -qq
!apt-get install -y -qq ffmpeg
!pip install -r requirements.txt
```

Nếu dùng tách người nói, đặt `HF_TOKEN` bằng Colab Secrets thay vì ghi token vào
notebook:

```python
import os
from google.colab import userdata

os.environ["HF_TOKEN"] = userdata.get("HF_TOKEN")
os.environ["DIARIZATION_DEVICE"] = "cuda"
```

### 2. Cài, khởi động Ollama và tải Gemma 4 E2B

```python
!curl -fsSL https://ollama.com/install.sh | sh
```

```python
import subprocess
import time

ollama_log = open("/content/ollama.log", "w")
ollama_process = subprocess.Popen(
    ["ollama", "serve"],
    stdout=ollama_log,
    stderr=subprocess.STDOUT,
)
time.sleep(3)
```

```python
!ollama pull gemma4:e2b
!ollama list
```

`gemma4:e2b` được app dùng cho bước tóm tắt. Nếu chọn **Gemma 4 E2B** trên giao
diện, model này cũng xử lý transcript audio. Không thêm `--api` nếu muốn dùng
Gemma để tóm tắt.

### 3. Chạy `app.py` để khởi động Flask

Đây chính là bước chạy `app.py`. Chạy cell này **một lần**, sau khi
`ollama pull gemma4:e2b` hoàn tất và trước khi chạy Cloudflare Tunnel:

```python
import requests

flask_log = open("/content/meetnote.log", "w")
flask_process = subprocess.Popen(
    ["python", "app.py", "--no-database"],
    stdout=flask_log,
    stderr=subprocess.STDOUT,
)
time.sleep(5)
!tail -n 30 /content/meetnote.log

if flask_process.poll() is not None:
    raise RuntimeError("app.py đã dừng. Xem lỗi trong /content/meetnote.log")

response = requests.get("http://127.0.0.1:5001", timeout=10)
print("Flask HTTP status:", response.status_code)
```

Kết quả phải là `Flask HTTP status: 200`. Lệnh trên dùng Ollama/Gemma để tóm
tắt và không đụng đến SQL Server. Nó tương đương với lệnh terminal:

```bash
python app.py --no-database
```

Nếu muốn Gemini tóm tắt thay cho Ollama, chỉ đổi danh sách lệnh trong cell thành:

```python
["python", "app.py", "--no-database", "--api"]
```

Đây là thay đổi cờ chạy, không phải sửa `app.py`. Khi dùng `--api`, cần đặt
`GEMINI_API_KEY` trong Colab Secrets. Ollama vẫn cần chạy nếu chọn **Gemma 4
E2B** làm engine transcript trên giao diện.

### 4. Tạo URL demo tạm thời

```python
!wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /usr/local/bin/cloudflared
!chmod +x /usr/local/bin/cloudflared
```

```python
!pkill -f cloudflared || true
```

Cell cuối cùng dùng HTTP/2 để tránh trường hợp kết nối QUIC từ Colab bị treo:

```python
!cloudflared tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:5001
```

Không dừng cell cuối. Sau vài giây, output sẽ có khung chứa dòng dạng:

```text
https://random-words.trycloudflare.com
```

Đó là link giao diện. Cell này phải tiếp tục hiện trạng thái đang chạy để giữ
tunnel hoạt động. Không chia sẻ rộng URL vì app chưa có đăng nhập. Giữ runtime
Colab, `flask_process`, `ollama_process` và cell Tunnel chạy suốt buổi demo.

Mỗi khi Colab bị `Disconnect and delete runtime`, phải chạy lại các cell theo
đúng thứ tự trên vì máy ảo và dữ liệu RAM đã bị xóa. Bạn vẫn không cần sửa bất
kỳ dòng nào trong `app.py`.

Trên T4, cấu hình dễ ổn định nhất là chọn **Gemma 4 E2B** và tắt diarization.
Whisper `large-v3`, pyannote và Ollama cùng giữ model trên GPU có thể vượt VRAM.
Nếu cần Whisper hoặc diarization, dùng `--api` cho phần tóm tắt hoặc ép
`DIARIZATION_DEVICE=cpu` để giảm tranh chấp VRAM.

## Zipformer 30M

Dropdown transcript có thêm `hynt/Zipformer-30M-RNNT-6000h`. Model dùng
`sherpa-onnx` giống NghiASR nên không cần cài thêm dependency. Lần sử dụng đầu
tiên, app tải bộ ONNX `int8` và token table từ Hugging Face; các lần sau dùng
cache trên máy. Có thể đổi cấu hình mà không sửa code:

```powershell
$env:ZIPFORMER_QUANTIZATION="int8" # hoặc fp32
$env:ZIPFORMER_NUM_THREADS="4"
python app.py
```

Model Zipformer này có giấy phép **CC BY-NC-ND 4.0**, vì vậy chỉ nên dùng cho
nghiên cứu/demo phi thương mại nếu chưa có giấy phép khác từ tác giả.

## PhoASR Whisper Small

Dropdown transcript có thêm `Qualcomm-AI-Research/PhoASR-whisper-small`, model
Whisper Small được fine-tune cho tiếng Việt. Model chạy qua Transformers, có
word timestamp và dùng chung sliding window 30 giây/overlap 5 giây cùng pipeline
gán speaker hiện có. Lần chạy đầu, Hugging Face tải gần 1 GB trọng số; các lần
sau dùng cache trên máy.

Thiết bị và kiểu dữ liệu được chọn tự động: CUDA dùng `float16`, CPU/MPS dùng
`float32`. Có thể cấu hình mà không sửa code:

```powershell
$env:PHOASR_DEVICE="cuda"       # auto, cuda, cpu hoặc mps
$env:PHOASR_DTYPE="float16"    # auto, float16, float32 hoặc bfloat16
python app.py
```

Checkpoint được phát hành theo BSD-3-Clause-Clear kèm Qualcomm Responsible AI
License và model card nêu mục đích nghiên cứu/giáo dục. Cần rà soát điều khoản
trước khi đưa vào sản phẩm thương mại, đồng thời ghi nguồn model nếu phân phối
phần mềm hoặc công bố kết quả.

## Tách người nói (Whisper, NghiASR, Zipformer và PhoASR)

Giao diện có tùy chọn **Tách người nói** cho Whisper, NghiASR, Zipformer và
PhoASR. Khi bật, app thực hiện pipeline sau:

1. Ghép các file theo đúng thứ tự tải lên và đổi toàn bộ cuộc họp thành WAV
   mono 16 kHz bằng FFmpeg.
2. Chạy `pyannote/speaker-diarization-community-1` trên toàn bộ cuộc họp để
   phân cụm người nói.
3. Bỏ các lượt speaker ngắn hơn ngưỡng trên giao diện (mặc định `2.0` giây)
   khỏi bước căn speaker; audio gốc không bị cắt bỏ.
4. Chạy model ASR đã chọn trên audio liên tục bằng sliding window 30 giây,
   overlap 5 giây. Mỗi cửa sổ chỉ giữ hypothesis thuộc vùng trung tâm của nó để
   không lặp chữ trong phần overlap.
5. Dùng word/token timestamps để gán kết quả ASR về lượt nói của pyannote và tạo
   transcript dạng `[00:00:05 - 00:00:12] người nói 1: ...`.

Có thể chỉnh hai tham số sliding window mà không sửa code:

```powershell
$env:DIARIZATION_ASR_WINDOW_SECONDS="30"
$env:DIARIZATION_ASR_OVERLAP_SECONDS="5"
python app.py
```

Overlap lớn hơn cung cấp thêm ngữ cảnh tại biên nhưng làm tăng thời gian xử lý.

### Thiết lập lần đầu

Chạy cài đặt dependency trong môi trường Python của app:

```powershell
pip install -r requirements.txt
```

Sau đó:

1. Đăng nhập Hugging Face và chấp nhận điều khoản tại
   `https://huggingface.co/pyannote/speaker-diarization-community-1`.
2. Tạo read token tại `https://huggingface.co/settings/tokens`.
3. Đặt token trong **cùng terminal sẽ chạy app**:

```powershell
$env:HF_TOKEN="hf_..."
python app.py --api
```

Nếu không muốn dùng PowerShell, mở **Edit environment variables for your
account** trong Windows, tạo biến người dùng `HF_TOKEN`, rồi đóng và mở lại
VS Code/terminal trước khi chạy app. Chỉ mở một terminal mới là chưa đủ nếu VS
Code đã được mở trước lúc tạo biến môi trường.

Model chỉ cần token để tải lần đầu và quá trình diarization diễn ra trên máy.
App mặc định đặt `PYANNOTE_METRICS_ENABLED=0`; người vận hành có thể tự đặt bằng
`1` trước khi chạy app nếu muốn bật telemetry ẩn danh của pyannote.
Máy hiện dùng bản PyTorch CPU nên xử lý các cuộc họp dài sẽ chậm; CUDA GPU được
chọn tự động nếu môi trường PyTorch nhận được GPU. Có thể ép thiết bị bằng biến
`DIARIZATION_DEVICE=cpu` hoặc `DIARIZATION_DEVICE=cuda`.

Nếu biết chính xác số người tham gia, nhập số đó trên giao diện để phân cụm ổn
định hơn. Ngưỡng `2.0` giây bám theo notebook tham chiếu nhưng có thể làm mất
các câu chen ngang ngắn; giảm ngưỡng nếu các câu này quan trọng. Pipeline mới
không bật loudness normalization để giữ hành vi audio hiện có của repo.

## Tình trạng SQL Server trên máy này

Đã kiểm tra ngày 05/08/2026:

- SQL Server default instance: `MSSQLSERVER` — đang `Running`, tự khởi động.
- Server name dùng trong SSMS và ứng dụng: `.` hoặc `localhost`.
- SQL Server Browser: không cần cho default instance chạy cùng máy.
- ODBC Driver 18 for SQL Server: đã cài.
- `pyodbc 5.3.0`: đã cài vào môi trường `evn_meet`.
- SSMS 22.8.2: `D:\SSMS\Common7\IDE\SSMS.exe`.
- TCP/IP và Named Pipes đang tắt; Shared Memory đang bật. Điều này đủ cho app
  và SQL Server chạy trên cùng máy.

## Thiết lập database lần đầu

### 1. Mở SSMS và kết nối

Mở:

```powershell
& "D:\SSMS\Common7\IDE\SSMS.exe"
```

Trong cửa sổ kết nối chọn:

- **Server type:** Database Engine
- **Server name:** `.`
- **Authentication:** Windows Authentication
- Bật **Trust server certificate** nếu SSMS hiển thị lựa chọn này.

Không cần nhập username/password khi dùng Windows Authentication.

### 2. Tạo database và bảng

Trong SSMS chọn `File → Open → File`, mở file `database.sql` của repo, sau đó
bấm **Execute** hoặc `F5`.

Khi thành công, Object Explorer sẽ có:

```text
Databases
└── MeetNote
    └── Tables
        └── dbo.MeetingHistory
```

Nếu chưa thấy, bấm chuột phải `Databases` và chọn `Refresh`.

### 3. Nhập lịch sử cũ (tùy chọn, chạy một lần)

```powershell
python scripts/migrate_results_to_sqlserver.py
```

Script đọc các file `results/*.txt` cũ. Những ID đã có trong database sẽ được
bỏ qua, không ghi đè nội dung đã sửa.

### 4. Chạy ứng dụng

Với Ollama:

```powershell
python app.py
```

Với Gemini:

```powershell
python app.py --api
```

Ứng dụng kiểm tra kết nối và bảng `dbo.MeetingHistory` trước khi mở cổng 5001.
Nếu database chưa được tạo hoặc đăng nhập thất bại, terminal sẽ báo nguyên nhân.

## Có cần mở SSMS mỗi lần không?

Không. SSMS chỉ là giao diện quản trị. Dịch vụ `MSSQLSERVER` mới là database
server và trên máy này nó đã được đặt `Automatic`.

Kiểm tra nhanh:

```powershell
Get-Service MSSQLSERVER
```

Nếu trạng thái không phải `Running`, mở PowerShell bằng quyền Administrator:

```powershell
Start-Service MSSQLSERVER
```

## Cấu hình kết nối

Mặc định [database.py](database.py) dùng:

```text
Driver: ODBC Driver 18 for SQL Server
Server: .
Database: MeetNote
Authentication: Windows Authentication
Encrypt: no
```

Không cần sửa code với cấu hình hiện tại. Nếu sau này dùng SQL Server
Authentication, đặt biến trong cùng PowerShell trước khi chạy app:

```powershell
$env:SQLSERVER_USERNAME="meetnote_user"
$env:SQLSERVER_PASSWORD="mật_khẩu"
python app.py --api
```

Các biến tùy chọn khác:

```text
SQLSERVER_SERVER
SQLSERVER_DATABASE
SQLSERVER_DRIVER
SQLSERVER_USERNAME
SQLSERVER_PASSWORD
```

## Bảng MeetingHistory

Bảng lưu UUID, tên báo cáo, thứ tự file audio, engine, transcript, biên bản,
timeline diarization, trạng thái, số file, tổng dung lượng, thời điểm tạo, hoàn
tất, chỉnh sửa cuối và cập nhật cuối. Sidebar hiển thị cả cuộc họp đang chờ duyệt
transcript, đang tóm tắt, tóm tắt lỗi và đã hoàn tất.

Khi bấm Lưu trong Markdown editor, `LastEditedAt` và `UpdatedAt` được cập nhật.
DOCX chỉ được tạo trong bộ nhớ khi người dùng bấm tải.

## Nếu kết nối local vẫn thất bại

Mở `SQLServerManager17.msc` bằng Run as administrator:

1. `SQL Server Network Configuration → Protocols for MSSQLSERVER`.
2. Bật `TCP/IP`.
3. Mở `TCP/IP → IP Addresses → IPAll`, giữ `TCP Port = 1433`.
4. Khởi động lại `SQL Server (MSSQLSERVER)`.
5. Chạy app với:

```powershell
$env:SQLSERVER_SERVER="tcp:localhost,1433"
python app.py --api
```
