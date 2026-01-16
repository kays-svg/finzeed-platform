import azure.functions as func
import logging
import json
import os
from datetime import datetime
import pyodbc
from azure.storage.blob import BlobServiceClient
from openai import AzureOpenAI
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
import re
from decimal import Decimal

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Configure logging with more detail
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database connection
def get_db_connection():
    connection_string = os.environ["SQL_CONNECTION_STRING"]
    return pyodbc.connect(connection_string)

# Blob Storage connection
def get_blob_service_client():
    connection_string = os.environ["AzureWebJobsStorage"]
    return BlobServiceClient.from_connection_string(connection_string)

def analyze_bank_statements(bank_statement_urls, declared_revenue):
    """
    Enhanced version with extensive logging for debugging
    """
    logger.info("=" * 80)
    logger.info("BANK STATEMENT ANALYSIS STARTED")
    logger.info(f"Number of statements to analyze: {len(bank_statement_urls)}")
    logger.info(f"Declared revenue: {declared_revenue:,.2f} EGP")
    logger.info("=" * 80)
    
    try:
        # Check if Document Intelligence credentials exist
        doc_intel_key = os.environ.get("DOCUMENT_INTELLIGENCE_KEY")
        doc_intel_endpoint = os.environ.get("DOCUMENT_INTELLIGENCE_ENDPOINT")
        
        logger.info(f"Document Intelligence Key exists: {bool(doc_intel_key)}")
        logger.info(f"Document Intelligence Endpoint: {doc_intel_endpoint}")
        
        if not doc_intel_key or not doc_intel_endpoint:
            logger.error("❌ Document Intelligence credentials missing!")
            return {
                "success": False,
                "error": "Document Intelligence not configured",
                "total_inflows": 0,
                "discrepancy_percentage": 0,
                "warning": None
            }
        
        # Initialize Document Intelligence client
        logger.info("Initializing Document Intelligence client...")
        document_analysis_client = DocumentAnalysisClient(
            endpoint=doc_intel_endpoint,
            credential=AzureKeyCredential(doc_intel_key)
        )
        logger.info("✓ Document Intelligence client initialized")
        
        all_transactions = []
        total_inflows = 0
        
        for idx, url in enumerate(bank_statement_urls, 1):
            logger.info("-" * 80)
            logger.info(f"Processing statement {idx}/{len(bank_statement_urls)}")
            logger.info(f"URL: {url[:100]}...")
            
            try:
                # Start Document Intelligence analysis
                logger.info(f"Starting Document Intelligence analysis for statement {idx}...")
                start_time = datetime.now()
                
                poller = document_analysis_client.begin_analyze_document_from_url(
                    "prebuilt-document", url
                )
                logger.info(f"Waiting for analysis results (this may take 30-60 seconds)...")
                
                result = poller.result()
                elapsed_time = (datetime.now() - start_time).total_seconds()
                logger.info(f"✓ Analysis completed in {elapsed_time:.2f} seconds")
                
                # Extract text
                full_text = ""
                if result.content:
                    full_text = result.content
                    logger.info(f"Extracted text length: {len(full_text)} characters")
                    logger.info(f"First 500 characters:\n{full_text[:500]}")
                else:
                    logger.warning(f"⚠️ No content extracted from statement {idx}")
                
                # Parse transactions
                logger.info(f"Parsing transactions from statement {idx}...")
                transactions = parse_transactions_from_text(full_text)
                logger.info(f"Found {len(transactions)} transactions")
                
                if transactions:
                    logger.info("Sample transactions (first 5):")
                    for i, trans in enumerate(transactions[:5], 1):
                        logger.info(f"  {i}. Amount: {trans['amount']:,.2f} EGP - Type: {trans['type']} - Description: {trans.get('description', 'N/A')[:50]}")
                
                all_transactions.extend(transactions)
                
                # Calculate inflows from this statement
                statement_inflows = sum(t['amount'] for t in transactions if t['type'] == 'credit')
                total_inflows += statement_inflows
                logger.info(f"Total inflows from statement {idx}: {statement_inflows:,.2f} EGP")
                
            except Exception as e:
                logger.error(f"❌ Error processing statement {idx}: {str(e)}")
                logger.exception("Full error details:")
                continue
        
        logger.info("=" * 80)
        logger.info("ANALYSIS SUMMARY")
        logger.info(f"Total transactions found: {len(all_transactions)}")
        logger.info(f"Total inflows calculated: {total_inflows:,.2f} EGP")
        logger.info(f"Declared revenue: {declared_revenue:,.2f} EGP")
        
        # Calculate discrepancy
        if declared_revenue > 0:
            discrepancy_percentage = ((declared_revenue - total_inflows) / declared_revenue) * 100
            logger.info(f"Discrepancy: {discrepancy_percentage:.1f}%")
        else:
            discrepancy_percentage = 0
            logger.info("Declared revenue is 0, cannot calculate discrepancy")
        
        # Determine if warning needed
        warning = None
        if discrepancy_percentage > 20:  # More than 20% discrepancy
            warning = f"⚠️ Revenue Discrepancy Detected: Bank statements show {total_inflows:,.2f} EGP in inflows, but declared revenue is {declared_revenue:,.2f} EGP (difference: {discrepancy_percentage:.1f}%)"
            logger.warning(warning)
        else:
            logger.info("✓ Revenue appears consistent with bank statements")
        
        result = {
            "success": True,
            "total_inflows": float(total_inflows),
            "declared_revenue": float(declared_revenue),
            "discrepancy_percentage": float(discrepancy_percentage),
            "transaction_count": len(all_transactions),
            "warning": warning
        }
        
        logger.info("=" * 80)
        logger.info(f"FINAL RESULT: {json.dumps(result, indent=2)}")
        logger.info("=" * 80)
        
        return result
        
    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR in bank statement analysis: {str(e)}")
        logger.exception("Full error trace:")
        return {
            "success": False,
            "error": str(e),
            "total_inflows": 0,
            "discrepancy_percentage": 0,
            "warning": None
        }

def parse_transactions_from_text(text):
    """
    Enhanced transaction parser with detailed logging
    """
    logger.info("Starting transaction parsing...")
    transactions = []
    
    # Pattern 1: Look for lines with amounts (numbers with commas or decimals)
    # Common formats:
    # - 50,000.00 or 50000.00 or 50,000 or 50000
    # - With CR/DR or Credit/Debit indicators
    
    amount_patterns = [
        # Pattern with CR/DR
        r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*(CR|DR|Credit|Debit)',
        # Pattern with keywords
        r'(Deposit|Credit|Transfer|Salary|Payment|Receipt|Income)\s+(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
        # Just large numbers (likely amounts)
        r'\b(\d{1,3}(?:,\d{3})+(?:\.\d{2})?)\b',
    ]
    
    logger.info(f"Searching text with {len(amount_patterns)} patterns...")
    
    for pattern_idx, pattern in enumerate(amount_patterns, 1):
        matches = re.finditer(pattern, text, re.IGNORECASE)
        match_count = 0
        
        for match in matches:
            match_count += 1
            try:
                # Extract amount
                amount_str = None
                transaction_type = 'credit'  # Default to credit
                description = ""
                
                if pattern_idx == 1:  # CR/DR pattern
                    amount_str = match.group(1)
                    indicator = match.group(2).upper()
                    transaction_type = 'credit' if 'CR' in indicator or 'CREDIT' in indicator else 'debit'
                    description = f"Transaction ({indicator})"
                    
                elif pattern_idx == 2:  # Keyword pattern
                    keyword = match.group(1)
                    amount_str = match.group(2)
                    transaction_type = 'credit'
                    description = keyword
                    
                else:  # Just amount
                    amount_str = match.group(1)
                    # Try to determine type from context
                    start_pos = max(0, match.start() - 50)
                    end_pos = min(len(text), match.end() + 50)
                    context = text[start_pos:end_pos].lower()
                    
                    credit_keywords = ['credit', 'deposit', 'receipt', 'income', 'salary', 'transfer in', 'cr']
                    debit_keywords = ['debit', 'withdrawal', 'payment', 'expense', 'transfer out', 'dr']
                    
                    if any(kw in context for kw in credit_keywords):
                        transaction_type = 'credit'
                        description = "Inflow"
                    elif any(kw in context for kw in debit_keywords):
                        transaction_type = 'debit'
                        description = "Outflow"
                    else:
                        # Assume large amounts are credits (inflows)
                        transaction_type = 'credit'
                        description = "Potential inflow"
                
                # Clean and convert amount
                amount_str = amount_str.replace(',', '')
                amount = float(amount_str)
                
                # Only consider significant amounts (> 1000 EGP)
                if amount > 1000:
                    transactions.append({
                        'amount': amount,
                        'type': transaction_type,
                        'description': description,
                        'pattern': pattern_idx
                    })
            
            except (ValueError, AttributeError) as e:
                logger.debug(f"Could not parse match: {match.group(0)} - {str(e)}")
                continue
        
        logger.info(f"Pattern {pattern_idx} found {match_count} matches")
    
    # Remove duplicates (same amount close together might be matched multiple times)
    logger.info(f"Total transactions before deduplication: {len(transactions)}")
    unique_transactions = []
    seen_amounts = set()
    
    for trans in transactions:
        amount_key = (trans['amount'], trans['type'])
        if amount_key not in seen_amounts:
            seen_amounts.add(amount_key)
            unique_transactions.append(trans)
    
    logger.info(f"Total unique transactions: {len(unique_transactions)}")
    
    return unique_transactions

@app.route(route="finzeed_ai_functions", methods=["POST"])
def finzeed_ai_functions(req: func.HttpRequest) -> func.HttpResponse:
    logger.info('=' * 80)
    logger.info('Finzeed AI Function triggered (Enhanced Logging Version)')
    logger.info('=' * 80)
    
    try:
        # Parse request
        req_body = req.get_json()
        logger.info(f"Request received with keys: {list(req_body.keys())}")
        
        company_name = req_body.get('companyName')
        contact_name = req_body.get('contactName')
        email = req_body.get('email')
        mobile = req_body.get('mobile')
        revenue = float(req_body.get('revenue', 0))
        requested_credit = float(req_body.get('requestedCredit', 0))
        
        logger.info(f"Company: {company_name}")
        logger.info(f"Revenue: {revenue:,.2f} EGP")
        logger.info(f"Requested Credit: {requested_credit:,.2f} EGP")
        
        # Handle file uploads
        files = req_body.get('files', [])
        logger.info(f"Number of files received: {len(files)}")
        
        blob_service_client = get_blob_service_client()
        container_name = "applications"
        
        # Ensure container exists
        try:
            container_client = blob_service_client.get_container_client(container_name)
            if not container_client.exists():
                container_client.create_container()
                logger.info(f"Created container: {container_name}")
        except Exception as e:
            logger.warning(f"Container check/create: {str(e)}")
        
        # Upload files and track bank statements
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        uploaded_files = []
        bank_statement_urls = []
        
        for idx, file_data in enumerate(files):
            file_name = file_data.get('name', f'file_{idx}')
            file_content = file_data.get('content', '')
            file_type = file_data.get('type', 'application/pdf')
            
            logger.info(f"Processing file {idx + 1}: {file_name} ({file_type})")
            
            # Decode base64
            import base64
            file_bytes = base64.b64decode(file_content.split(',')[1] if ',' in file_content else file_content)
            
            # Upload to blob storage
            blob_name = f"{company_name.replace(' ', '_')}_{timestamp}_{file_name}"
            blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
            blob_client.upload_blob(file_bytes, overwrite=True)
            
            blob_url = blob_client.url
            logger.info(f"✓ Uploaded: {blob_name}")
            
            uploaded_files.append({
                'name': file_name,
                'url': blob_url,
                'type': file_type
            })
            
            # Track bank statements
            if 'bank' in file_name.lower() or 'statement' in file_name.lower():
                bank_statement_urls.append(blob_url)
                logger.info(f"  → Identified as bank statement")
        
        logger.info(f"Total bank statements identified: {len(bank_statement_urls)}")
        
        # Analyze bank statements if any were uploaded
        bank_analysis = None
        if bank_statement_urls:
            logger.info("\n" + "=" * 80)
            logger.info("STARTING BANK STATEMENT ANALYSIS")
            logger.info("=" * 80 + "\n")
            bank_analysis = analyze_bank_statements(bank_statement_urls, revenue)
            logger.info("\nBANK ANALYSIS COMPLETE\n")
        else:
            logger.warning("⚠️ No bank statements found in uploaded files")
        
        # AI Assessment using OpenAI
        logger.info("Starting AI assessment...")
        client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_KEY"],
            api_version="2024-02-15-preview",
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"]
        )
        
        # Build prompt with bank analysis results
        bank_analysis_text = ""
        if bank_analysis and bank_analysis.get('success'):
            bank_analysis_text = f"""
BANK STATEMENT ANALYSIS RESULTS:
- Total inflows from statements: {bank_analysis['total_inflows']:,.2f} EGP
- Declared revenue: {bank_analysis['declared_revenue']:,.2f} EGP
- Discrepancy: {bank_analysis['discrepancy_percentage']:.1f}%
- Transactions analyzed: {bank_analysis.get('transaction_count', 0)}
{f"- WARNING: {bank_analysis['warning']}" if bank_analysis.get('warning') else "- Revenue verification: PASSED"}
"""
        
        prompt = f"""You are Finzeed's AI credit analyst. Assess this application:

Company: {company_name}
Annual Revenue: {revenue:,.2f} EGP
Requested Credit: {requested_credit:,.2f} EGP
{bank_analysis_text}

Assessment Criteria:
- Minimum revenue: 3,000,000 EGP
- Interest rate: 3.5% per month (42% annual) for all tiers
- Credit tiers:
  * 10M+ revenue: 18 months term, max 5M credit
  * 5M-10M revenue: 12 months term, max 3M credit
  * 3M-5M revenue: 6 months term, max 1.5M credit
  * <3M revenue: UNDER REVIEW

Provide assessment with:
1. Status: APPROVED, UNDER_REVIEW, or REJECTED
2. Credit limit (if applicable)
3. Term (if applicable)
4. Interest rate
5. Brief professional explanation
{f"6. Address the revenue discrepancy if present" if bank_analysis and bank_analysis.get('warning') else ""}

Format as JSON."""

        logger.info("Sending prompt to OpenAI...")
        response = client.chat.completions.create(
            model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=500
        )
        
        ai_response = response.choices[0].message.content
        logger.info(f"AI Response received: {ai_response[:200]}...")
        
        # Parse AI response
        try:
            assessment = json.loads(ai_response)
        except:
            # Fallback parsing
            assessment = {"status": "UNDER_REVIEW", "explanation": ai_response}
        
        # Add bank analysis warning to response if needed
        if bank_analysis and bank_analysis.get('warning'):
            assessment['bank_statement_warning'] = bank_analysis['warning']
            logger.warning(f"Adding bank statement warning to response: {bank_analysis['warning']}")
        
        # Save to database
        logger.info("Saving application to database...")
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO applications (
                company_name, contact_name, email, mobile,
                revenue, requested_credit, ai_assessment, status,
                submission_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company_name, contact_name, email, mobile,
            revenue, requested_credit, json.dumps(assessment),
            assessment.get('status', 'PENDING'),
            datetime.now()
        ))
        
        application_id = cursor.execute("SELECT @@IDENTITY").fetchone()[0]
        
        # Save documents
        for file_info in uploaded_files:
            cursor.execute("""
                INSERT INTO documents (application_id, document_type, file_name, file_url, upload_date)
                VALUES (?, ?, ?, ?, ?)
            """, (
                application_id,
                'bank_statement' if 'bank' in file_info['name'].lower() else 'other',
                file_info['name'],
                file_info['url'],
                datetime.now()
            ))
        
        conn.commit()
        conn.close()
        
        logger.info(f"✓ Application saved with ID: {application_id}")
        logger.info('=' * 80)
        logger.info('Request completed successfully')
        logger.info('=' * 80)
        
        return func.HttpResponse(
            json.dumps({
                "assessment": assessment,
                "application_id": application_id,
                "files_uploaded": len(uploaded_files),
                "bank_analysis": bank_analysis
            }),
            mimetype="application/json",
            status_code=200
        )
        
    except Exception as e:
        logger.error(f"❌ ERROR in main function: {str(e)}")
        logger.exception("Full error trace:")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500
        )
