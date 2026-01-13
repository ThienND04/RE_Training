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

def clean_garbage_lines(text):
    """
    Loại bỏ các dòng rác thường gặp trong file trắc nghiệm copy từ web/PDF
    để AI tập trung vào nội dung chính.
    """
    lines = text.split('\n')
    cleaned_lines = []
    
    # Các cụm từ rác cần loại bỏ
    garbage_phrases = [
        "group of answer choices", 
        "studocu", 
        "downloaded by", 
        "--- page",
        "trắc nghiệm kỹ nghệ yêu cầu",
        "thu thập và phân tích yêu cầu"
    ]
    
    for line in lines:
        content = line.strip()
        # Bỏ dòng trống
        if not content: continue
        
        # Kiểm tra rác
        is_garbage = False
        content_lower = content.lower()
        for trash in garbage_phrases:
            if trash in content_lower:
                is_garbage = True
                break
        
        # Loại bỏ các dòng chỉ có 1-2 ký tự (trừ khi là A. B. C. D.)
        if len(content) < 3 and not re.match(r'^[a-dA-D][\.\)]', content):
            is_garbage = True

        if not is_garbage:
            cleaned_lines.append(line)
            
    return cleaned_lines

def split_lines_into_chunks(lines, lines_per_chunk=60, overlap_lines=15):
    """
    Chia danh sách dòng đã làm sạch thành các đoạn nhỏ.
    
    CHIẾN THUẬT MỚI:
    - lines_per_chunk=60: Mỗi lần chỉ gửi khoảng 8-10 câu hỏi. AI sẽ không bị quá tải và ít bỏ sót hơn.
    - overlap_lines=15: Đảm bảo câu hỏi ở biên luôn an toàn.
    """
    total_lines = len(lines)
    chunks = []
    
    step = lines_per_chunk - overlap_lines
    if step < 1: step = 1

    for i in range(0, total_lines, step):
        selected_lines = lines[i : i + lines_per_chunk]
        chunk_content = "\n".join(selected_lines)
        chunks.append(chunk_content)
        
        if i + lines_per_chunk >= total_lines:
            break
            
    return chunks

def extract_quiz_from_chunk(model, chunk_text, chunk_index):
    """
    Gửi đoạn text lên Gemini để trích xuất JSON.
    """
    prompt = f"""
    Bạn là một máy trích xuất dữ liệu nghiêm ngặt.
    Nhiệm vụ: Chỉ trích xuất các câu hỏi trắc nghiệm CÓ THỰC trong văn bản dưới đây.
    TUYỆT ĐỐI KHÔNG SÁNG TÁC HAY TỰ TẠO CÂU HỎI KHÔNG CÓ TRONG VĂN BẢN.

    INPUT TEXT:
    \"\"\"
    {chunk_text}
    \"\"\"

    YÊU CẦU XỬ LÝ:
    1. Chỉ trích xuất những câu hỏi có đầy đủ nội dung và các lựa chọn đáp án từ văn bản gốc.
    2. Nếu câu hỏi bị cắt cụt (mất phần đầu hoặc phần đuôi) do giới hạn văn bản, HÃY BỎ QUA NÓ NGAY LẬP TỨC.
    3. Giữ nguyên văn nội dung câu hỏi và đáp án, chỉ sửa lỗi chính tả nhỏ nếu cần thiết.
    4. Tự động xác định đáp án đúng (correct index) và giải thích (explanation) dựa trên kiến thức của bạn.

    OUTPUT JSON (Array):
    [
      {{
        "question": "Nội dung câu hỏi gốc...",
        "options": ["A", "B", "C", "D"],
        "correct": 0,
        "explanation": "..."
      }}
    ]
    Chỉ trả về JSON thuần, không markdown.
    """
    
    try:
        response = model.generate_content(prompt)
        text_resp = response.text.strip()
        
        # Xử lý chuỗi JSON trả về để loại bỏ markdown code block
        # Sử dụng logic kiểm tra đơn giản và an toàn hơn
        if text_resp.startswith("```json"):
            text_resp = text_resp[7:]
        elif text_resp.startswith("```"):
            text_resp = text_resp[3:]
            
        if text_resp.endswith("```"):
            text_resp = text_resp[:-3]
            
        text_resp = text_resp.strip()
        
        return json.loads(text_resp)
    except Exception as e:
        print(f"  [Chunk {chunk_index}] Cảnh báo: Không trích xuất được dữ liệu ({e})")
        return []

def normalize_text(text):
    text = re.sub(r'[^\w\s]', '', text) 
    return re.sub(r'\s+', ' ', text).strip().lower()

def is_duplicate(question_obj, existing_list):
    new_q = normalize_text(question_obj['question'])
    if len(new_q) < 15: return True # Bỏ qua câu quá ngắn hoặc lỗi

    for item in existing_list:
        existing_q = normalize_text(item['question'])
        # So sánh độ tương đồng > 85%
        ratio = difflib.SequenceMatcher(None, new_q, existing_q).ratio()
        if ratio > 0.85: 
            return True
    return False

def main():
    input_filename = "TN.txt"
    output_filename = "Du_lieu_trac_nghiem_Full_Gemini_Final.json"
    
    print(f"--- CÔNG CỤ TẠO DATA TRẮC NGHIỆM V3 (SẠCH & CHI TIẾT) ---")
    
    api_key = input("Nhập Google Gemini API Key: ").strip()
    if not api_key: return

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-3-pro-preview')
    
    # 1. Đọc file
    raw_text = read_file_content(input_filename)
    if not raw_text: return

    # 2. Làm sạch rác
    print("-> Đang làm sạch các dòng rác (header, footer)...")
    cleaned_lines = clean_garbage_lines(raw_text)
    print(f"-> Đã lọc xong. Còn lại {len(cleaned_lines)} dòng nội dung sạch.")

    # 3. Chia nhỏ (60 dòng/chunk)
    chunks = split_lines_into_chunks(cleaned_lines, lines_per_chunk=60, overlap_lines=15)
    print(f"-> Chia thành {len(chunks)} đoạn nhỏ để xử lý kỹ càng.")
    
    all_questions = []
    
    # 4. Xử lý từng đoạn
    for i, chunk in enumerate(chunks):
        print(f"Đang xử lý đoạn {i+1}/{len(chunks)}...", end=" ")
        
        batch = extract_quiz_from_chunk(model, chunk, i+1)
        
        added = 0
        if batch:
            for q in batch:
                if not is_duplicate(q, all_questions):
                    q["priority"] = 10
                    all_questions.append(q)
                    added += 1
            print(f"-> Thêm {added} câu (Tổng: {len(all_questions)})")
        else:
            print("-> (Trống/Lặp)")
            
        time.sleep(1.0) # Nghỉ nhẹ

    # 5. Lưu kết quả
    print("\n--- Đang lưu file... ---")
    try:
        with open(output_filename, 'w', encoding='utf-8') as f:
            json.dump(all_questions, f, ensure_ascii=False, indent=2)
        print(f"HOÀN TẤT! Tổng cộng {len(all_questions)} câu hỏi.")
        print(f"File kết quả: {output_filename}")
    except Exception as e:
        print(f"Lỗi ghi file: {e}")

if __name__ == "__main__":
    main()