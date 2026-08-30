"""
LOCAL TEST SCRIPT - No Deployment Needed
Tests different Document Intelligence extraction methods to find best approach

Run this locally to see which method extracts the most text from your PDF
"""

import os
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential

# Your Azure credentials (from environment or paste here)
DOC_INTEL_ENDPOINT = os.environ.get("DOCUMENT_INTELLIGENCE_ENDPOINT") or "YOUR_ENDPOINT"
DOC_INTEL_KEY = os.environ.get("DOCUMENT_INTELLIGENCE_KEY") or "YOUR_KEY"

# Path to your CIB bank statement PDF
PDF_PATH = "09-Statements_31JAN25_to_31DEC25.pdf"

def test_extraction_methods(pdf_path):
    """Test different Document Intelligence extraction methods"""
    
    client = DocumentAnalysisClient(
        endpoint=DOC_INTEL_ENDPOINT,
        credential=AzureKeyCredential(DOC_INTEL_KEY)
    )
    
    print("="*80)
    print("TESTING DOCUMENT INTELLIGENCE EXTRACTION METHODS")
    print("="*80)
    
    with open(pdf_path, "rb") as f:
        pdf_content = f.read()
    
    print(f"\n📄 PDF Size: {len(pdf_content):,} bytes")
    
    # =========================================================================
    # METHOD 1: prebuilt-read (current method)
    # =========================================================================
    print("\n" + "="*80)
    print("METHOD 1: prebuilt-read")
    print("="*80)
    
    with open(pdf_path, "rb") as f:
        poller = client.begin_analyze_document("prebuilt-read", document=f)
        result = poller.result()
    
    text_from_content = result.content if result.content else ""
    
    print(f"📝 Text from content: {len(text_from_content):,} characters")
    print(f"📊 Tables found: {len(result.tables) if result.tables else 0}")
    
    if result.tables:
        total_cells = sum(len(list(table.cells)) for table in result.tables)
        print(f"📋 Total cells in tables: {total_cells:,}")
    
    # =========================================================================
    # METHOD 2: prebuilt-read + extract from tables
    # =========================================================================
    print("\n" + "="*80)
    print("METHOD 2: prebuilt-read + extract from tables")
    print("="*80)
    
    full_text_method2 = text_from_content
    
    if result.tables:
        for table in result.tables:
            for cell in table.cells:
                full_text_method2 += " " + cell.content
    
    print(f"📝 Combined text: {len(full_text_method2):,} characters")
    print(f"✅ Improvement: +{len(full_text_method2) - len(text_from_content):,} characters")
    
    # =========================================================================
    # METHOD 3: prebuilt-layout
    # =========================================================================
    print("\n" + "="*80)
    print("METHOD 3: prebuilt-layout")
    print("="*80)
    
    with open(pdf_path, "rb") as f:
        poller = client.begin_analyze_document("prebuilt-layout", document=f)
        result_layout = poller.result()
    
    text_from_layout = result_layout.content if result_layout.content else ""
    
    print(f"📝 Text from content: {len(text_from_layout):,} characters")
    print(f"📊 Tables found: {len(result_layout.tables) if result_layout.tables else 0}")
    
    # =========================================================================
    # METHOD 4: prebuilt-layout + tables
    # =========================================================================
    print("\n" + "="*80)
    print("METHOD 4: prebuilt-layout + extract from tables")
    print("="*80)
    
    full_text_method4 = text_from_layout
    
    if result_layout.tables:
        for table in result_layout.tables:
            for cell in table.cells:
                full_text_method4 += " " + cell.content
    
    print(f"📝 Combined text: {len(full_text_method4):,} characters")
    
    # =========================================================================
    # COMPARISON
    # =========================================================================
    print("\n" + "="*80)
    print("COMPARISON")
    print("="*80)
    
    methods = {
        "Method 1 (prebuilt-read)": len(text_from_content),
        "Method 2 (prebuilt-read + tables)": len(full_text_method2),
        "Method 3 (prebuilt-layout)": len(text_from_layout),
        "Method 4 (prebuilt-layout + tables)": len(full_text_method4)
    }
    
    for method, chars in methods.items():
        print(f"{method:40s}: {chars:>10,} characters")
    
    # Find best method
    best_method = max(methods, key=methods.get)
    best_chars = methods[best_method]
    
    print("\n" + "="*80)
    print(f"🏆 WINNER: {best_method}")
    print(f"📝 Extracted: {best_chars:,} characters")
    print("="*80)
    
    # Show sample of best extraction
    if best_method == "Method 1 (prebuilt-read)":
        sample_text = text_from_content
    elif best_method == "Method 2 (prebuilt-read + tables)":
        sample_text = full_text_method2
    elif best_method == "Method 3 (prebuilt-layout)":
        sample_text = text_from_layout
    else:
        sample_text = full_text_method4
    
    print("\n📄 Sample of extracted text (first 500 chars):")
    print("-" * 80)
    print(sample_text[:500])
    print("-" * 80)
    
    # Count transaction keywords
    print("\n🔍 Transaction keyword analysis:")
    keywords = {
        "IPN Inward": sample_text.count("IPN Inward"),
        "Cash Deposit": sample_text.count("Cash Deposit"),
        "Account Transfer Collection": sample_text.count("Account Transfer Collection"),
        "Outward": sample_text.count("Outward")
    }
    
    for keyword, count in keywords.items():
        print(f"  {keyword:30s}: {count:>5} occurrences")
    
    print("\n✅ Testing complete!")
    print(f"📊 Recommendation: Use {best_method}")
    
    return best_method, sample_text


if __name__ == "__main__":
    # Run the test
    best_method, text = test_extraction_methods(PDF_PATH)
    
    print("\n" + "="*80)
    print("NEXT STEPS")
    print("="*80)
    print(f"1. Update your function_app.py to use: {best_method}")
    print(f"2. Expected result: Extract {len(text):,} characters instead of 3,327")
    print(f"3. Should find 150+ transactions instead of 7")
    print("="*80)
