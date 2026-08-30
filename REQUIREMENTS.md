# Finzeed Platform - Product Requirements Document

**Version:** 1.0
**Date:** 2026-04-01
**Owner:** Khaled Emad ElKaysouny

---

## Vision

Build the most intelligent AI-powered SME working capital financing platform in Egypt. Finzeed combines Anthropic Claude and OpenAI to deliver institutional-grade credit assessment for small and medium enterprises, enabling fast, data-driven financing decisions.

---

## Platform Architecture

```
                         www.finzeed.com
                               |
               +---------------+---------------+
               |                               |
     [Customer Portal]              [Backoffice Portal]
      (Public Website)              (Internal Admin)
               |                               |
               +----------- API ---------------+
                               |
                    +----------+----------+
                    |                     |
             [Anthropic Claude]     [Azure OpenAI]
              Credit Analysis        Document Analysis
                    |                     |
                    +----------+----------+
                               |
                     [Azure SQL Database]
                     [Azure Blob Storage]
```

---

## 1. Customer Portal (www.finzeed.com)

### 1.1 Current Features (Completed)
- [x] Landing page with product information (How it works, Why Finzeed)
- [x] Supplier marketplace
- [x] User registration with email verification (auto-verify fallback)
- [x] User login with JWT authentication
- [x] Credit application form with document uploads
- [x] Bank statement analysis via Azure Document Intelligence + OpenAI
- [x] Credit assessment with revenue-tiered decision engine
- [x] AI chat assistant (Azure OpenAI)
- [x] User profile with application history
- [x] Credit line renewal flow with installment calculator
- [x] Results modal showing assessment details

### 1.2 New Requirements

#### 1.2.1 Contact Us
- Add a "Contact Us" link in the navbar and footer
- Email: contact@finzeed.com (or configured email)
- Optional: Contact form that sends email via SendGrid
- Display: phone number, email, office address

#### 1.2.2 Credit Decision Flow (Changed)
**Current behavior:** Customer sees credit decision immediately (APPROVED / UNDER_REVIEW / REJECTED)

**New behavior:**
- Customer submits application with documents
- AI performs analysis in the background
- Customer sees: **"Application Submitted Successfully"** (never sees APPROVED/REJECTED directly)
- Customer gets email notification **only when Finzeed team approves** via backoffice
- Application status visible in profile: `Submitted` → `Under Review` → `Approved` / `Rejected`
- Customer portal shows status but NOT the AI recommendation (that's internal only)

#### 1.2.3 Enhanced Document Collection
Customer must upload:
| Document | Required | Format | Purpose |
|----------|----------|--------|---------|
| Bank statements (3-12 months) | Yes | PDF | Revenue verification, cashflow analysis |
| Balance sheet (latest) | Yes | PDF/Image | Financial health, assets/liabilities |
| Commercial registration | Yes | PDF/Image | Legal verification |
| Tax card | Recommended | PDF/Image | Tax compliance |
| National ID | Yes | PDF/Image | Identity verification, personal data extraction |
| Credit bureau report (i-Score) | If available | PDF | Credit history, existing obligations |

#### 1.2.4 Customer Data Input
In addition to documents, customer provides:
- Company name, industry, years in business
- Annual revenue (declared)
- Monthly inflows (declared)
- Business description / activity
- Supplier(s) they want to pay through Finzeed
- Requested credit amount and tenor

#### 1.2.5 Domain Configuration
- Primary domain: **www.finzeed.com**
- Apex domain: **finzeed.com** (redirect to www)
- SSL: Managed certificate via Azure Static Web Apps
- Current: `ashy-tree-0dadc2203.6.azurestaticapps.net` → needs CNAME to www.finzeed.com

---

## 2. Backoffice Portal (Internal Admin)

### 2.1 Overview
A separate internal web application for the Finzeed operations team to manage credit applications, review AI recommendations, approve/reject requests, and manage platform data.

**Access:** Authenticated Finzeed team members only (role-based access)

### 2.2 Dashboard
- Total applications (today / this week / this month)
- Applications by status (Pending Review / Approved / Rejected)
- Total credit disbursed
- Revenue metrics
- Recent activity feed

### 2.3 Application Management

#### 2.3.1 Application List
- Filterable table: status, date range, company, decision, credit amount
- Search by company name, email, application ID
- Sort by date, amount, AI confidence score
- Quick status indicators (color-coded)

#### 2.3.2 Application Detail View
For each application, the backoffice shows:

**Customer Information:**
- Full name, email, mobile, company
- National ID data (extracted from uploaded ID via AI)
- Business description and industry

**Uploaded Documents:**
- View/download all uploaded documents
- Document viewer (PDF/image preview)
- Document status (Received / Verified / Issue)

**AI Credit Assessment Report:**
- AI recommendation: APPROVE / REJECT / REVIEW
- Recommended credit limit and tenor
- Confidence score
- Risk factors identified
- Revenue verification (declared vs bank statement analysis)
- Balance sheet analysis summary
- i-Score analysis (if provided)
- Inflow consistency analysis
- Business risk profile

**Insurance Documents (added by team):**
- Upload insurance-related documents
- Insurance status tracking
- Notes field for insurance team

**Decision Section:**
- Approve / Reject buttons
- Override AI recommendation (with reason)
- Set final credit limit (can differ from AI recommendation)
- Set final tenor and terms
- Add internal notes
- Approval triggers email notification to customer

#### 2.3.3 Application Workflow
```
Customer Submits → AI Analysis (automatic) → Pending Review
                                                   |
                                    Backoffice Team Reviews
                                                   |
                              +--------------------+--------------------+
                              |                    |                    |
                          Approved            Request Info          Rejected
                              |                    |                    |
                    Email to Customer    Email requesting docs   Email to Customer
                    (with terms)         (specify what's needed) (with reason)
```

### 2.4 Customer Management
- Customer list with search/filter
- Customer detail view (all applications, documents, history)
- Edit customer information
- Flag/block customers

### 2.5 Supplier Management
- Add / edit / remove suppliers shown on the customer portal
- Supplier fields: name, category, location, invoice range, payment terms, status (Active/Pilot/Preferred)
- Changes reflect immediately on the customer-facing suppliers page
- Supplier verification status

### 2.6 Reports & Analytics
- Credit portfolio overview
- Approval/rejection rates
- Average credit amount by industry
- AI accuracy tracking (AI recommendation vs final decision)
- Monthly disbursement trends

### 2.7 User Management (Admin)
- Add/remove backoffice users
- Role-based access: Admin, Credit Analyst, Viewer
- Audit log of all actions

---

## 3. AI Integration - Intelligent Credit Assessment

### 3.1 Architecture
Dual-AI approach for maximum accuracy:

```
Customer Data + Documents
         |
         v
+------------------+     +-------------------+
| Azure OpenAI     |     | Anthropic Claude   |
| (GPT-4)          |     | (Claude Opus/Sonnet)|
|                  |     |                    |
| - Document text  |     | - Credit analysis  |
|   extraction     |     | - Risk assessment  |
| - Bank statement |     | - Decision engine  |
|   parsing        |     | - Report generation|
| - Transaction    |     | - ID extraction    |
|   classification |     | - Cross-validation |
+------------------+     +-------------------+
         |                         |
         +----------+--------------+
                    |
            Combined AI Report
                    |
              Stored in DB
                    |
         Shown in Backoffice
```

### 3.2 Data Points Analyzed

#### Bank Statements (Primary)
- Total inflows / outflows per month
- Average monthly revenue
- Revenue trend (growing / stable / declining)
- Transaction frequency and patterns
- Large/irregular transactions flagged
- Seasonal patterns
- Bounce/return patterns
- Average account balance

#### Balance Sheet
- Total assets vs liabilities
- Current ratio (liquidity)
- Debt-to-equity ratio
- Working capital position
- Fixed assets valuation
- Inventory levels (if applicable)

#### Credit Bureau - i-Score (If Available)
- Credit score interpretation
- Existing loan obligations
- Payment history with other lenders
- Number of credit inquiries
- Outstanding defaults or delays

#### National ID Analysis
- Extract: full name, date of birth, address, national ID number
- Cross-reference with application data
- Age verification
- Address verification

#### Business Assessment
- Industry risk profile
- Years in business
- Revenue consistency (declared vs verified)
- Supplier relationships
- Business model viability

### 3.3 AI Credit Report Output

The AI generates a structured credit report for the backoffice:

```json
{
  "recommendation": "APPROVE | REJECT | REVIEW",
  "confidence_score": 85,
  "recommended_credit_limit": 1500000,
  "recommended_tenor_months": 12,
  "monthly_rate": 3.5,

  "revenue_analysis": {
    "declared_annual": 10000000,
    "verified_annual": 9500000,
    "discrepancy_percent": 5,
    "monthly_trend": "growing",
    "revenue_verdict": "VERIFIED"
  },

  "bank_analysis": {
    "total_inflows": 9500000,
    "total_outflows": 7200000,
    "avg_monthly_balance": 450000,
    "transactions_analyzed": 156,
    "months_covered": 6,
    "bounce_count": 0,
    "seasonal_pattern": "Q4 peak"
  },

  "balance_sheet_analysis": {
    "total_assets": 5000000,
    "total_liabilities": 2000000,
    "current_ratio": 1.8,
    "debt_to_equity": 0.67,
    "working_capital": 1200000,
    "verdict": "HEALTHY"
  },

  "iscore_analysis": {
    "score": 720,
    "existing_obligations": 500000,
    "payment_history": "GOOD",
    "defaults": 0,
    "verdict": "LOW_RISK"
  },

  "identity_verification": {
    "name_match": true,
    "id_number": "***masked***",
    "age": 35,
    "address_match": "PARTIAL"
  },

  "risk_factors": [
    "High credit utilization on existing facility",
    "Single supplier concentration"
  ],

  "positive_factors": [
    "100% payment punctuality",
    "Growing revenue trend",
    "Strong current ratio",
    "Clean i-Score"
  ],

  "executive_summary": "Strong SME with verified revenue of 9.5M EGP, healthy balance sheet, and clean credit history. Recommend approval at 1.5M EGP for 12 months. Low risk profile with growing business trajectory."
}
```

### 3.4 Anthropic Claude Integration

**Purpose:** Primary credit analysis engine and report generation

**Use cases:**
1. **Credit Risk Assessment** - Analyze all customer data points and generate a comprehensive credit recommendation
2. **Document Cross-Validation** - Compare data across bank statements, balance sheet, and declared information
3. **National ID Extraction** - Extract personal data from ID images
4. **Executive Summary** - Generate human-readable credit report for the backoffice team
5. **Anomaly Detection** - Flag inconsistencies or suspicious patterns

**API:** Anthropic Messages API with Claude Opus/Sonnet

### 3.5 Azure OpenAI Integration

**Purpose:** Document text extraction and structured data parsing

**Use cases:**
1. **Bank Statement Parsing** - Extract transactions, categorize inflows/outflows
2. **Balance Sheet Parsing** - Extract financial figures from uploaded documents
3. **i-Score Report Parsing** - Extract credit score and obligations
4. **Chat Assistant** - Customer-facing chat on the website

**API:** Azure OpenAI Service (GPT-4)

---

## 4. Technical Stack

| Component | Technology |
|-----------|-----------|
| Customer Portal | HTML/CSS/JS (Azure Static Web Apps) |
| Backoffice Portal | React or HTML/JS (Azure Static Web Apps) |
| Backend API | Azure Functions (Python) |
| AI - Credit Analysis | Anthropic Claude API |
| AI - Document Parsing | Azure OpenAI (GPT-4) |
| Document OCR | Azure Document Intelligence |
| Database | Azure SQL Database |
| File Storage | Azure Blob Storage |
| Email | SendGrid |
| Domain | www.finzeed.com (Azure DNS / Custom domain) |
| Monitoring | Application Insights |

---

## 5. Database Schema Updates

### New Tables Needed

```sql
-- Backoffice users
CREATE TABLE backoffice_users (
    id INT IDENTITY PRIMARY KEY,
    email NVARCHAR(255) UNIQUE NOT NULL,
    password_hash NVARCHAR(256),
    password_salt NVARCHAR(64),
    fullname NVARCHAR(255),
    role NVARCHAR(50) DEFAULT 'analyst', -- admin, analyst, viewer
    is_active BIT DEFAULT 1,
    created_at DATETIME DEFAULT GETDATE(),
    last_login DATETIME
);

-- AI credit reports (stored per application)
CREATE TABLE credit_reports (
    id INT IDENTITY PRIMARY KEY,
    application_id INT REFERENCES applications(id),
    ai_recommendation NVARCHAR(50),
    confidence_score DECIMAL(5,2),
    recommended_limit DECIMAL(18,2),
    recommended_tenor INT,
    revenue_analysis NVARCHAR(MAX),  -- JSON
    bank_analysis NVARCHAR(MAX),     -- JSON
    balance_sheet_analysis NVARCHAR(MAX), -- JSON
    iscore_analysis NVARCHAR(MAX),   -- JSON
    identity_verification NVARCHAR(MAX), -- JSON
    risk_factors NVARCHAR(MAX),      -- JSON
    positive_factors NVARCHAR(MAX),  -- JSON
    executive_summary NVARCHAR(MAX),
    created_at DATETIME DEFAULT GETDATE()
);

-- Application decisions (by backoffice team)
CREATE TABLE application_decisions (
    id INT IDENTITY PRIMARY KEY,
    application_id INT REFERENCES applications(id),
    decided_by INT REFERENCES backoffice_users(id),
    decision NVARCHAR(50), -- APPROVED, REJECTED, REQUEST_INFO
    final_credit_limit DECIMAL(18,2),
    final_tenor_months INT,
    final_interest_rate DECIMAL(5,2),
    override_reason NVARCHAR(MAX),
    internal_notes NVARCHAR(MAX),
    customer_notified BIT DEFAULT 0,
    decided_at DATETIME DEFAULT GETDATE()
);

-- Insurance documents (uploaded by team)
CREATE TABLE insurance_documents (
    id INT IDENTITY PRIMARY KEY,
    application_id INT REFERENCES applications(id),
    document_type NVARCHAR(100),
    filename NVARCHAR(255),
    blob_url NVARCHAR(500),
    uploaded_by INT REFERENCES backoffice_users(id),
    notes NVARCHAR(MAX),
    uploaded_at DATETIME DEFAULT GETDATE()
);

-- Suppliers (managed via backoffice)
CREATE TABLE suppliers (
    id INT IDENTITY PRIMARY KEY,
    name NVARCHAR(255) NOT NULL,
    category NVARCHAR(100),
    location NVARCHAR(255),
    invoice_range_min DECIMAL(18,2),
    invoice_range_max DECIMAL(18,2),
    payment_terms NVARCHAR(100),
    status NVARCHAR(50) DEFAULT 'Active', -- Active, Pilot, Preferred, Inactive
    is_verified BIT DEFAULT 0,
    created_at DATETIME DEFAULT GETDATE(),
    updated_at DATETIME DEFAULT GETDATE()
);

-- Audit log
CREATE TABLE audit_log (
    id INT IDENTITY PRIMARY KEY,
    user_id INT,
    user_type NVARCHAR(20), -- 'customer' or 'backoffice'
    action NVARCHAR(100),
    entity_type NVARCHAR(50),
    entity_id INT,
    details NVARCHAR(MAX),
    ip_address NVARCHAR(50),
    created_at DATETIME DEFAULT GETDATE()
);
```

### Applications Table Updates
```sql
ALTER TABLE applications ADD
    application_status NVARCHAR(50) DEFAULT 'submitted',
    -- submitted, ai_reviewed, pending_review, approved, rejected, info_requested
    customer_notified BIT DEFAULT 0,
    notification_sent_at DATETIME,
    requested_amount DECIMAL(18,2),
    requested_installments INT,
    national_id_number NVARCHAR(50),
    years_in_business INT,
    business_description NVARCHAR(MAX),
    declared_monthly_inflows DECIMAL(18,2);
```

---

## 6. Implementation Phases

### Phase 1: Enhanced Customer Portal (Week 1-2)
- [ ] Add Contact Us section (email link, contact form)
- [ ] Change credit decision flow (hide AI result from customer, show "Submitted")
- [ ] Add National ID upload field to credit form
- [ ] Add i-Score upload field (optional)
- [ ] Add business description and years in business fields
- [ ] Add declared monthly inflows field
- [ ] Configure www.finzeed.com custom domain
- [ ] Set up SendGrid for transactional emails
- [ ] Update application status display in profile (Submitted → Under Review → Approved)

### Phase 2: Anthropic Claude Integration (Week 2-3)
- [ ] Set up Anthropic API integration
- [ ] Build comprehensive credit analysis prompt
- [ ] Implement dual-AI pipeline (OpenAI for extraction, Claude for analysis)
- [ ] Generate structured AI credit reports
- [ ] Store AI reports in database
- [ ] National ID data extraction via Claude Vision
- [ ] Balance sheet analysis
- [ ] i-Score interpretation
- [ ] Cross-validation logic (bank vs declared vs balance sheet)

### Phase 3: Backoffice Portal (Week 3-5)
- [ ] Build backoffice authentication system
- [ ] Dashboard with KPIs and activity feed
- [ ] Application list with filters and search
- [ ] Application detail view with AI report
- [ ] Document viewer (PDF/image preview)
- [ ] Approve/Reject workflow with email notifications
- [ ] Insurance document upload section
- [ ] Supplier management (CRUD)
- [ ] Customer management
- [ ] Audit logging

### Phase 4: Polish & Production (Week 5-6)
- [ ] Email notifications (approval, rejection, info request)
- [ ] Reports and analytics
- [ ] Role-based access control
- [ ] Security hardening
- [ ] Performance optimization
- [ ] User acceptance testing
- [ ] Production deployment

---

## 7. API Endpoints

### Customer Portal API (Existing + New)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/finzeed_ai_functions` | Main endpoint (existing) |
| | `action=register` | User registration |
| | `action=login` | User login |
| | `action=profile` | Get profile + history |
| | `action=contact` | Submit contact form (NEW) |
| | `multipart/form-data` | Credit application with files |

### Backoffice API (New)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/backoffice` | Backoffice main endpoint |
| | `action=login` | Admin login |
| | `action=dashboard` | Dashboard stats |
| | `action=list_applications` | Get applications (filtered) |
| | `action=get_application` | Full application detail + AI report |
| | `action=approve_application` | Approve with terms |
| | `action=reject_application` | Reject with reason |
| | `action=request_info` | Request additional docs |
| | `action=upload_insurance_doc` | Upload insurance document |
| | `action=list_suppliers` | Get suppliers |
| | `action=add_supplier` | Add supplier |
| | `action=update_supplier` | Update supplier |
| | `action=delete_supplier` | Remove supplier |
| | `action=list_customers` | Get customers |
| | `action=get_customer` | Customer detail |
| | `action=audit_log` | View audit trail |

---

## 8. Security Requirements

- All API endpoints authenticated (JWT)
- Backoffice requires separate auth with role-based access
- Sensitive data encrypted at rest (Azure SQL TDE)
- Documents stored in Azure Blob with SAS token access
- National ID numbers masked in logs and UI (show last 4 digits)
- CORS restricted to specific origins
- Rate limiting on public endpoints
- Audit logging for all backoffice actions
- No hardcoded secrets (all via Azure App Settings)
- HTTPS enforced on all endpoints

---

## 9. Environment Variables (Required)

```
# Existing
SQL_CONNECTION_STRING=...
AzureWebJobsStorage=...
DOCUMENT_INTELLIGENCE_ENDPOINT=...
DOCUMENT_INTELLIGENCE_KEY=...
AZURE_OPENAI_ENDPOINT=...
AZURE_OPENAI_KEY=...
AZURE_OPENAI_DEPLOYMENT=finzeed-chat
JWT_SECRET=...

# New
ANTHROPIC_API_KEY=...          # Claude API key
SENDGRID_API_KEY=...           # Email service
SENDGRID_SENDER_EMAIL=noreply@finzeed.com
SITE_URL=https://www.finzeed.com
BACKOFFICE_URL=https://admin.finzeed.com
BACKOFFICE_JWT_SECRET=...      # Separate secret for admin auth
```
