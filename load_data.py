import json
import time
import re
import os
import difflib

# Cài đặt thư viện: pip install google-generativeai
try:
    import google.generativeai as genai
except ImportError:
    print("Lỗi: Chưa cài đặt thư viện 'google-generativeai'.")
    print("Vui lòng chạy lệnh: pip install google-generativeai")
    exit(1)

def read_file_content(filepath):
    """Đọc toàn bộ nội dung file text."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"LỖI: Không tìm thấy file '{filepath}'. Hãy chắc chắn bạn đã đổi tên file dữ liệu thành '{filepath}'.")
        return None

def split_text_with_overlap(text, pages_per_chunk=3, overlap_pages=1):
    """
    Chia văn bản thành các đoạn có sự chồng lấn (overlap).
    Chiến thuật:
    - Chunk 1: Trang 1, 2, 3
    - Chunk 2: Trang 3, 4, 5 (Trang 3 được lặp lại để bắt các câu hỏi nằm giữa trang 2 và 3, hoặc 3 và 4)
    """
    # 1. Tách trang dựa trên dấu hiệu '--- PAGE ... ---'
    pages = re.split(r'--- PAGE \d+ ---', text)
    pages = [p.strip() for p in pages if p.strip()] # Lọc trang rỗng
    
    # 2. Fallback: Nếu không tìm thấy marker phân trang (file chỉ là 1 cục text dài)
    if len(pages) <= 1:
        print("-> Cảnh báo: Không thấy dấu hiệu phân trang '--- PAGE'. Chuyển sang chia theo số lượng ký tự...")
        # Chia theo ký tự: Mỗi chunk 8000 ký tự, overlap 1500 ký tự
        chunk_size = 8000
        overlap_char = 1500
        chunks = []
        if not text: return []
        for i in range(0, len(text), chunk_size - overlap_char):
            chunks.append(text[i : i + chunk_size])
        return chunks

    # 3. Chia theo trang có overlap
    chunks = []
    total_pages = len(pages)
    
    # Bước nhảy = Số trang mỗi chunk - Số trang muốn lặp lại
    step = pages_per_chunk - overlap_pages
    if step < 1: step = 1

    for i in range(0, total_pages, step):
        # Lấy slice các trang. Ví dụ: từ trang 0 đến trang 3
        selected_pages = pages[i : i + pages_per_chunk]
        
        # Gộp nội dung các trang lại thành 1 chunk
        chunk_content = "\n\n".join(selected_pages)
        chunks.append(chunk_content)
        
        # Nếu đã quét hết trang thì dừng
        if i + pages_per_chunk >= total_pages:
            break
            
    return chunks

def extract_quiz_from_chunk(model, chunk_text, chunk_index):
    """
    Gửi đoạn text lên Gemini để trích xuất JSON.
    """
    prompt = f"""
    Bạn là một chuyên gia xử lý dữ liệu từ văn bản OCR lỗi.
    
    INPUT TEXT:
    \"\"\"
    {chunk_text}
    \"\"\"

    NHIỆM VỤ:
    1. Trích xuất danh sách câu hỏi trắc nghiệm từ văn bản trên.
    2. Tự động sửa lỗi chính tả, lỗi dính chữ (ví dụ: "A.Option1B.Option2" -> tách ra).
    3. Tự động chọn đáp án đúng (correct index) và viết giải thích (explanation) ngắn gọn.
    4. QUAN TRỌNG: Nếu một câu hỏi bị cắt cụt ở đầu hoặc cuối văn bản (do hết trang), HÃY BỎ QUA NÓ. Chúng tôi sẽ lấy nó ở lần xử lý chunk tiếp theo (do có cơ chế chồng lấn).

    OUTPUT FORMAT (JSON Array only):
    [
      {{
        "question": "Nội dung câu hỏi đầy đủ?",
        "options": ["Đáp án A", "Đáp án B", "Đáp án C", "Đáp án D"],
        "correct": 0,
        "explanation": "Giải thích tại sao..."
      }}
    ]
    Không thêm markdown ```json, chỉ trả về raw text JSON.
    """
    
    try:
        response = model.generate_content(prompt)
        text_resp = response.text.strip()
        
        # Làm sạch các ký tự markdown thừa nếu AI lỡ thêm vào
        # Sử dụng biến tạm để tránh lỗi hiển thị trên chat
        marker_json = "```json"
        marker_code = "```"
        
        if text_resp.startswith(marker_json): 
            text_resp = text_resp[len(marker_json):]
        elif text_resp.startswith(marker_code): 
            text_resp = text_resp[len(marker_code):]
            
        if text_resp.endswith(marker_code): 
            text_resp = text_resp[:-len(marker_code)]
        
        return json.loads(text_resp.strip())
    except Exception as e:
        print(f"  [Chunk {chunk_index}] Lỗi xử lý hoặc không tìm thấy JSON hợp lệ: {e}")
        return []

def normalize_text(text):
    """Chuẩn hóa chuỗi để so sánh: chữ thường, bỏ khoảng trắng thừa."""
    return re.sub(r'\s+', ' ', text).strip().lower()

def is_duplicate(question_obj, existing_list):
    """
    Kiểm tra trùng lặp. Do chúng ta gửi lặp lại các trang (overlap), 
    chắc chắn sẽ có câu hỏi trùng. Cần lọc bỏ.
    """
    new_q = normalize_text(question_obj['question'])
    
    for item in existing_list:
        existing_q = normalize_text(item['question'])
        
        # So sánh độ tương đồng (Fuzzy Matching)
        # Nếu giống nhau > 90% thì coi là trùng
        ratio = difflib.SequenceMatcher(None, new_q, existing_q).ratio()
        if ratio > 0.9: 
            return True
    return False

def main():
    # --- CẤU HÌNH ---
    input_filename = "TN.txt"  # File đầu vào cố định
    output_filename = "Du_lieu_trac_nghiem_Full_Gemini_Final.json"
    
    print(f"--- CÔNG CỤ TẠO DATA TRẮC NGHIỆM TỪ '{input_filename}' ---")
    
    # 1. Nhập Key
    api_key = input("Nhập Google Gemini API Key của bạn: ").strip()
    if not api_key: 
        print("Chưa nhập API Key. Thoát.")
        return

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    # 2. Đọc file
    raw_text = read_file_content(input_filename)
    if not raw_text: return

    # 3. Chia đoạn (Overlap 1 trang)
    # Chunk 1: Trang 1, 2, 3 -> Chunk 2: Trang 3, 4, 5
    chunks = split_text_with_overlap(raw_text, pages_per_chunk=3, overlap_pages=1)
    print(f"Tổng số đoạn cần xử lý: {len(chunks)} (bao gồm phần chồng lấn).")
    
    all_questions = []
    
    # 4. Loop xử lý
    for i, chunk in enumerate(chunks):
        print(f"Đang xử lý đoạn {i+1}/{len(chunks)}...", end=" ")
        
        # Gọi Gemini
        batch = extract_quiz_from_chunk(model, chunk, i+1)
        
        # Lọc trùng và thêm vào danh sách tổng
        added_count = 0
        if batch:
            for q in batch:
                if not is_duplicate(q, all_questions):
                    q["priority"] = 10 # Gán priority mặc định
                    all_questions.append(q)
                    added_count += 1
            print(f"-> Nhận {len(batch)} câu, thêm mới {added_count} câu.")
        else:
            print("-> Không có dữ liệu.")
            
        time.sleep(2) # Nghỉ 2s để tránh bị chặn

    # 5. Lưu file
    print("\n--- Đang ghi file kết quả... ---")
    try:
        with open(output_filename, 'w', encoding='utf-8') as f:
            json.dump(all_questions, f, ensure_ascii=False, indent=2)
        print(f"HOÀN TẤT! Tổng cộng {len(all_questions)} câu hỏi duy nhất.")
        print(f"File kết quả: {output_filename}")
    except Exception as e:
        print(f"Lỗi ghi file: {e}")

if __name__ == "__main__":
    main()