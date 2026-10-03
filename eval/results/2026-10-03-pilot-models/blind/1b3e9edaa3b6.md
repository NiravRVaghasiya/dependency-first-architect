# Multi-Tenant SaaS Billing System Architecture

I'll architect a comprehensive billing system for you. Let me break this down into key components:

## 1. **System Architecture Overview**

```
┌─────────────────────────────────────────────────────────────┐
│                     Client Applications                      │
│              (Web Dashboard, Mobile, APIs)                   │
└────────────────┬────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────┐
│                    API Gateway & Auth                        │
│          (API Rate Limiting, OAuth, JWT, TLS)                │
└────────────────┬────────────────────────────────────────────┘
                 │
      ┌──────────┼──────────┬────────────┐
      │          │          │            │
      ▼          ▼          ▼            ▼
  ┌────────┐┌──────────┐┌─────────┐┌──────────┐
  │Billing ││Metering  ││Invoicing││Reporting│
  │Service ││Service   ││Service  ││Service   │
  └───┬────┘└──┬───────┘└────┬────┘└──┬───────┘
      │         │             │        │
      └─────────┼─────────────┼────────┘
                │             │
      ┌─────────▼──────┬──────▼────────┐
      │                │               │
      ▼                ▼               ▼
  ┌────────────┐ ┌──────────────┐ ┌──────────┐
  │  Payment   │ │   Event Bus  │ │ Cache    │
  │  Processor │ │  (Kafka)     │ │ (Redis)  │
  └────┬───────┘ └──────────────┘ └──────────┘
       │
       ▼
  ┌──────────────────────┐
  │  External Gateways   │
  │  (Stripe, Paddle)    │
  └──────────────────────┘
```

## 2. **Core Components**

### A. **Billing Service**
```java
// Core Billing Service Interface
public interface IBillingService {
    // Subscription Management
    Subscription createSubscription(String tenantId, SubscriptionRequest req);
    void upgradeSubscription(String subscriptionId, String newPlanId);
    void downgradeSubscription(String subscriptionId, String newPlanId);
    void cancelSubscription(String subscriptionId, CancelReason reason);
    
    // Billing Cycles
    Invoice generateInvoice(String subscriptionId, Period period);
    List<Invoice> getInvoiceHistory(String subscriptionId, int limit);
    void sendInvoice(String invoiceId);
    
    // Payment Processing
    PaymentResult processPayment(String invoiceId, PaymentDetails details);
    void handleWebhook(PaymentProvider provider, WebhookPayload payload);
    
    // Usage & Overages
    void recordUsage(String tenantId, UsageEvent event);
    List<Overages> calculateOverages(String subscriptionId, Period period);
}
```

### B. **Metering Service**
```java
public interface IMeteringService {
    // Real-time Usage Tracking
    void recordMetric(String tenantId, MetricData metric);
    MetricUsage getUsage(String tenantId, String metricName, DateRange period);
    List<MetricUsage> getAggregatedUsage(String tenantId, DateRange period);
    
    // Rate Limiting
    RateLimitCheck checkRateLimit(String tenantId, String feature);
    void resetQuota(String tenantId, String feature);
}
```

### C. **Invoice & Reporting Service**
```java
public interface IInvoicingService {
    Invoice createInvoice(InvoiceData data);
    void addLineItems(String invoiceId, List<LineItem> items);
    void applyDisounts(String invoiceId, DiscountCode discount);
    void finalizeInvoice(String invoiceId);
    byte[] generatePDF(String invoiceId);
    List<Invoice> searchInvoices(InvoiceFilter filter);
}

public interface IReportingService {
    RevenueReport generateRevenueReport(DateRange period);
    ChurnAnalysis getChurnMetrics(DateRange period);
    MRRDashboard getMRRMetrics();
    CustomerLifetimeValue calculateCLTV(String customerId);
}
```

## 3. **Data Model & Database Schema**

### **Multi-Tenant Isolation Strategy**

```sql
-- 1. TENANT MANAGEMENT
CREATE TABLE tenants (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    status ENUM('active', 'suspended', 'deleted'),
    billing_email VARCHAR(255),
    created_at TIMESTAMP,
    deleted_at TIMESTAMP,
    metadata JSONB
);

-- 2. SUBSCRIPTION PLANS
CREATE TABLE plans (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    name VARCHAR(255),
    description TEXT,
    price_model ENUM('flat', 'per_unit', 'tiered', 'volume'),
    currency VARCHAR(3),
    recurring_interval ENUM('month', 'year', 'week'),
    features JSONB,
    created_at TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    INDEX idx_tenant_plans (tenant_id)
);

-- 3. SUBSCRIPTIONS
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    customer_id UUID NOT NULL,
    plan_id UUID NOT NULL,
    status ENUM('active', 'canceled', 'past_due', 'paused'),
    billing_cycle_anchor TIMESTAMP,
    current_period_start TIMESTAMP,
    current_period_end TIMESTAMP,
    cancel_at TIMESTAMP,
    canceled_at TIMESTAMP,
    cancellation_reason TEXT,
    metadata JSONB,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    FOREIGN KEY (plan_id) REFERENCES plans(id),
    INDEX idx_tenant_subs (tenant_id, customer_id),
    INDEX idx_status (status)
);

-- 4. INVOICES
CREATE TABLE invoices (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    subscription_id UUID NOT NULL,
    customer_id UUID NOT NULL,
    status ENUM('draft', 'sent', 'paid', 'overdue', 'voided'),
    amount_due DECIMAL(19,4),
    amount_paid DECIMAL(19,4),
    currency VARCHAR(3),
    period_start TIMESTAMP,
    period_end TIMESTAMP,
    due_date TIMESTAMP,
    paid_at TIMESTAMP,
    invoice_number VARCHAR(50),
    pdf_url VARCHAR(500),
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id),
    INDEX idx_tenant_invoices (tenant_id, created_at),
    INDEX idx_status (status, due_date)
);

-- 5. LINE ITEMS
CREATE TABLE line_items (
    id UUID PRIMARY KEY,
    invoice_id UUID NOT NULL,
    description VARCHAR(500),
    quantity DECIMAL(19,4),
    unit_price DECIMAL(19,4),
    amount DECIMAL(19,4),
    type ENUM('subscription', 'overage', 'addon', 'credit'),
    tax_rate DECIMAL(5,4),
    FOREIGN KEY (invoice_id) REFERENCES invoices(id),
    ON DELETE CASCADE
);

-- 6. USAGE METRICS (Time-Series)
CREATE TABLE usage_events (
    tenant_id UUID NOT NULL,
    customer_id UUID NOT NULL,
    metric_name VARCHAR(100),
    quantity DECIMAL(19,4),
    timestamp TIMESTAMP,
    metadata JSONB,
    INDEX idx_usage (tenant_id, customer_id, metric_name, timestamp)
) PARTITION BY RANGE (YEAR(timestamp));

-- 7. PAYMENTS
CREATE TABLE payments (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    invoice_id UUID NOT NULL,
    external_payment_id VARCHAR(255),
    payment_method_id UUID,
    amount DECIMAL(19,4),
    currency VARCHAR(3),
    status ENUM('pending', 'completed', 'failed', 'refunded'),
    provider ENUM('stripe', 'paddle', 'adyen'),
    created_at TIMESTAMP,
    processed_at TIMESTAMP,
    idempotency_key VARCHAR(255),
    UNIQUE(idempotency_key),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    INDEX idx_payments (tenant_id, created_at)
);

-- 8. WEBHOOKS LOG
CREATE TABLE webhook_logs (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    provider VARCHAR(50),
    event_type VARCHAR(100),
    payload LONGTEXT,
    processed BOOLEAN,
    error_message TEXT,
    created_at TIMESTAMP,
    processed_at TIMESTAMP,
    INDEX idx_webhooks (tenant_id, provider, created_at)
);
```

## 4. **API Design**

### **Billing API Endpoints**

```
# Subscriptions
POST   /api/v1/subscriptions              # Create subscription
GET    /api/v1/subscriptions/{id}         # Get subscription
PATCH  /api/v1/subscriptions/{id}         # Update subscription
DELETE /api/v1/subscriptions/{id}         # Cancel subscription
POST   /api/v1/subscriptions/{id}/upgrade # Upgrade plan
POST   /api/v1/subscriptions/{id}/downgrade # Downgrade plan

# Invoices
GET    /api/v1/invoices                   # List invoices
GET    /api/v1/invoices/{id}              # Get invoice
GET    /api/v1/invoices/{id}/pdf          # Download PDF
POST   /api/v1/invoices/{id}/send         # Send invoice
POST   /api/v1/invoices/{id}/void         # Void invoice

# Usage & Metering
POST   /api/v1/usage/record               # Record usage event
GET    /api/v1/usage/current              # Get current usage
GET    /api/v1/usage/history              # Get usage history

# Plans
GET    /api/v1/plans                      # List plans
POST   /api/v1/plans                      # Create plan
PUT    /api/v1/plans/{id}                 # Update plan

# Payments
POST   /api/v1/payments                   # Create payment
GET    /api/v1/payments/{id}              # Get payment status
POST   /api/v1/webhooks/{provider}        # Webhook endpoint

# Reports
GET    /api/v1/reports/revenue            # Revenue metrics
GET    /api/v1/reports/mrr                # MRR dashboard
GET    /api/v1/reports/churn              # Churn analysis
```

### **Request/Response Example**

```json
// POST /api/v1/subscriptions
{
  "customer_id": "cust_123",
  "plan_id": "plan_pro",
  "billing_cycle_anchor": "2026-10-15",
  "payment_method_id": "pm_visa_123",
  "metadata": {
    "company": "Acme Corp",
    "department": "Engineering"
  }
}

// Response
{
  "id": "sub_abc123",
  "customer_id": "cust_123",
  "plan_id": "plan_pro",
  "status": "active",
  "current_period_start": "2026-10-03",
  "current_period_end": "2026-11-03",
  "amount": 99.00,
  "currency": "USD",
  "next_billing_date": "2026-11-03",
  "created_at": "2026-10-03T10:30:00Z"
}
```

## 5. **Pricing Models Implementation**

```java
public interface IPricingCalculator {
    BigDecimal calculatePrice(Subscription sub, UsageData usage);
}

// Flat Rate
public class FlatRatePricingCalculator implements IPricingCalculator {
    @Override
    public BigDecimal calculatePrice(Subscription sub, UsageData usage) {
        return sub.getPlan().getPrice();
    }
}

// Per-Unit
public class PerUnitPricingCalculator implements IPricingCalculator {
    @Override
    public BigDecimal calculatePrice(Subscription sub, UsageData usage) {
        long quantity = usage.getMetricValue("api_calls");
        BigDecimal unitPrice = sub.getPlan().getUnitPrice();
        return unitPrice.multiply(new BigDecimal(quantity));
    }
}

// Tiered Pricing
public class TieredPricingCalculator implements IPricingCalculator {
    @Override
    public BigDecimal calculatePrice(Subscription sub, UsageData usage) {
        long quantity = usage.getMetricValue("seats");
        List<Tier> tiers = sub.getPlan().getTiers();
        
        BigDecimal totalPrice = BigDecimal.ZERO;
        long previousQuantity = 0;
        
        for (Tier tier : tiers) {
            long tierMax = Math.min(quantity, tier.getUpTo());
            long tierQuantity = tierMax - previousQuantity;
            
            if (tierQuantity > 0) {
                BigDecimal tierPrice = tier.getPrice()
                    .multiply(new BigDecimal(tierQuantity));
                totalPrice = totalPrice.add(tierPrice);
            }
            
            previousQuantity = tierMax;
            if (quantity <= tier.getUpTo()) break;
        }
        
        return totalPrice;
    }
}

// Usage-Based with Overage
public class UsageBasedCalculator implements IPricingCalculator {
    @Override
    public BigDecimal calculatePrice(Subscription sub, UsageData usage) {
        BigDecimal basePrice = sub.getPlan().getPrice();
        
        long usedQuantity = usage.getMetricValue("requests");
        long includedQuantity = sub.getPlan().getIncludedUsage();
        
        if (usedQuantity > includedQuantity) {
            long overageQuantity = usedQuantity - includedQuantity;
            BigDecimal overagePrice = sub.getPlan().getOverageUnitPrice()
                .multiply(new BigDecimal(overageQuantity));
            return basePrice.add(overagePrice);
        }
        
        return basePrice;
    }
}
```

## 6. **Event-Driven Architecture**

```java
public enum BillingEvent {
    SUBSCRIPTION_CREATED,
    SUBSCRIPTION_UPGRADED,
    SUBSCRIPTION_DOWNGRADED,
    SUBSCRIPTION_CANCELED,
    BILLING_CYCLE_STARTED,
    INVOICE_GENERATED,
    INVOICE_SENT,
    PAYMENT_RECEIVED,
    PAYMENT_FAILED,
    USAGE_THRESHOLD_EXCEEDED,
    OVERAGE_RECORDED
}

public interface IEventBus {
    void publish(BillingEvent event, EventPayload payload);
    void subscribe(BillingEvent event, EventHandler handler);
}

// Event Handlers
public class InvoiceGeneratedEventHandler {
    public void handle(EventPayload payload) {
        Invoice invoice = payload.getInvoice();
        
        // Send email notification
        emailService.sendInvoiceNotification(invoice);
        
        // Update analytics
        analyticsService.recordInvoiceGenerated(invoice);
        
        // Trigger payment reminders
        reminderService.schedulePaymentReminder(invoice);
    }
}

public class PaymentReceivedEventHandler {
    public void handle(EventPayload payload) {
        Payment payment = payload.getPayment();
        Invoice invoice = payment.getInvoice();
        
        // Mark invoice as paid
        invoice.setStatus(InvoiceStatus.PAID);
        invoiceRepository.save(invoice);
        
        // Send receipt
        emailService.sendReceipt(invoice, payment);
        
        // Update metrics
        revenueService.recordPayment(payment);
    }
}
```

## 7. **Billing Cycle Management**

```java
@Service
public class BillingCycleService {
    
    @Scheduled(cron = "0 0 * * * *") // Every hour
    public void processBillingCycles() {
        List<Subscription> dueSubs = subscriptionRepository
            .findDueForBilling(LocalDateTime.now());
        
        for (Subscription subscription : dueSubs) {
            processBillingCycle(subscription);
        }
    }
    
    private void processBillingCycle(Subscription subscription) {
        try {
            // Calculate charges
            BillingCalculation calc = calculateCharges(subscription);
            
            // Create invoice
            Invoice invoice = createInvoice(subscription, calc);
            
            // Record line items
            for (LineItem item : calc.getLineItems()) {
                invoice.addLineItem(item);
            }
            
            // Apply discounts/credits
            applyPromotions(subscription, invoice);
            
            // Finalize
            invoice.setStatus(InvoiceStatus.SENT);
            invoiceRepository.save(invoice);
            
            // Process payment
            PaymentResult result = paymentProcessor.process(invoice);
            
            if (result.isSuccess()) {
                eventBus.publish(BillingEvent.PAYMENT_RECEIVED, result);
            } else {
                handlePaymentFailure(invoice, result);
            }
            
        } catch (Exception e) {
            logger.error("Billing cycle failed for subscription: " 
                + subscription.getId(), e);
            subscription.setStatus(SubscriptionStatus.PAST_DUE);
            subscriptionRepository.save(subscription);
        }
    }
    
    private BillingCalculation calculateCharges(Subscription subscription) {
        BillingCalculation calc = new BillingCalculation();
        
        // Base subscription charge
        calc.addLineItem(new LineItem(
            "Subscription: " + subscription.getPlan().getName(),
            1,
            subscription.getPlan().getPrice(),
            LineItemType.SUBSCRIPTION
        ));
        
        // Usage overages
        UsageData usage = metersService.getUsageSince(
            subscription.getTenantId(),
            subscription.getCurrentPeriodStart()
        );
        
        BigDecimal overageAmount = calculateOverages(subscription, usage);
        if (overageAmount.compareTo(BigDecimal.ZERO) > 0) {
            calc.addLineItem(new LineItem(
                "Overage Charges",
                1,
                overageAmount,
                LineItemType.OVERAGE
            ));
        }
        
        return calc;
    }
}
```

## 8. **Multi-Tenancy Implementation**

```java
public class TenantContext {
    private static final ThreadLocal<String> TENANT_ID = new ThreadLocal<>();
    
    public static void setTenantId(String tenantId) {
        TENANT_ID.set(tenantId);
    }
    
    public static String getTenantId() {
        return TENANT_ID.get();
    }
    
    public static void clear() {
        TENANT_ID.remove();
    }
}

@Component
public class TenantInterceptor implements HandlerInterceptor {
    
    @Override
    public boolean preHandle(HttpServletRequest request, 
                            HttpServletResponse response, 
                            Object handler) throws Exception {
        String tenantId = extractTenantId(request);
        if (tenantId == null) {
            response.sendError(HttpServletResponse.SC_UNAUTHORIZED, 
                "Tenant ID required");
            return false;
        }
        
        TenantContext.setTenantId(tenantId);
        return true;
    }
    
    private String extractTenantId(HttpServletRequest request) {
        // From header, JWT claim, or subdomain
        return request.getHeader("X-Tenant-ID");
    }
}

@Component
public class TenantAwareRepositoryImpl implements TenantAwareRepository {
    
    @Override
    public <T> List<T> findByTenant(Class<T> entityClass) {
        String tenantId = TenantContext.getTenantId();
        return findByTenantId(entityClass, tenantId);
    }
}
```

## 9. **Payment Processing Integration**

```java
@Service
public class PaymentProcessorService {
    
    public PaymentResult processPayment(Invoice invoice) {
        Subscription subscription = invoice.getSubscription();
        PaymentMethodId paymentMethod = subscription.getPaymentMethodId();
        
        PaymentRequest request = new PaymentRequest()
            .setAmount(invoice.getAmountDue())
            .setCurrency(invoice.getCurrency())
            .setPaymentMethodId(paymentMethod.getId())
            .setIdempotencyKey(UUID.randomUUID().toString())
            .setMetadata(Map.of(
                "invoice_id", invoice.getId(),
                "tenant_id", invoice.getTenantId()
            ));
        
        try {
            PaymentProvider provider = getProvider(paymentMethod);
            PaymentResponse response = provider.charge(request);
            
            Payment payment = new Payment()
                .setInvoiceId(invoice.getId())
                .setAmount(invoice.getAmountDue())
                .setExternalPaymentId(response.getPaymentId())
                .setStatus(PaymentStatus.COMPLETED)
                .setProvider(provider.getType());
            
            paymentRepository.save(payment);
            eventBus.publish(BillingEvent.PAYMENT_RECEIVED, payment);
            
            return PaymentResult.success(payment);
            
        } catch (PaymentException e) {
            return handlePaymentFailure(invoice, e);
        }
    }
    
    public void handleWebhook(String provider, Map<String, Object> payload) {
        String eventType = (String) payload.get("type");
        
        switch(eventType) {
            case "payment.completed":
                handlePaymentCompleted(payload);
                break;
            case "payment.failed":
                handlePaymentFailed(payload);
                break;
            case "payment.dispute":
                handlePaymentDispute(payload);
                break;
        }
    }
}
```

## 10. **Scaling & Performance**

### **Caching Strategy**
```java
@Service
public class CachedPricingService {
    
    @Cacheable(value = "plan_prices", key = "#planId")
    public PlanPrice getPlanPrice(String planId) {
        return pricingRepository.findById(planId);
    }
    
    @Cacheable(value = "usage", key = "#tenantId + ':' + #month")
    public UsageSummary getMonthlyUsage(String tenantId, YearMonth month) {
        return metersService.getUsageForMonth(tenantId, month);
    }
    
    @CacheEvict(value = "usage", key = "#tenantId")
    public void invalidateUsageCache(String tenantId) {}
}
```

### **Database Optimization**
```sql
-- Partition invoices by creation date for faster queries
CREATE TABLE invoices_2026_10 PARTITION OF invoices
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');

-- Index for common queries
CREATE INDEX idx_active_subscriptions ON subscriptions(tenant_id, status) 
    WHERE status = 'active';

-- Materialized view for reporting
CREATE MATERIALIZED VIEW mv_monthly_revenue AS
SELECT 
    tenant_id,
    DATE_TRUNC('month', created_at) AS month,
    SUM(amount_paid) as revenue,
    COUNT(DISTINCT customer_id) as paying_customers
FROM invoices
WHERE status = 'paid'
GROUP BY tenant_id, DATE_TRUNC('month', created_at);
```

## 11. **Security Considerations**

- **Payment PCI Compliance**: Use tokenized payments (Stripe, Paddle)
- **Data Encryption**: Encrypt sensitive fields at rest
- **Row-Level Security**: Use database policies for multi-tenancy
- **API Authentication**: OAuth 2.0 + JWT tokens
- **Rate Limiting**: Prevent abuse on billing APIs
- **Audit Logging**: Track all financial transactions
- **Webhook Verification**: Validate signatures from payment providers

## 12. **Monitoring & Alerts**

```yaml
Metrics to Track:
  - MRR (Monthly Recurring Revenue)
  - ARR (Annual Recurring Revenue)
  - Churn Rate
  - Customer Lifetime Value (CLTV)
  - Payment Success Rate
  - Invoice Generation Time (SLA)
  - Processing Latency
  - Webhook Failure Rate
  
Alerts:
  - High payment failure rate (> 5%)
  - Failed billing cycles
  - Webhook processing delays
  - Database query performance degradation
  - Revenue anomalies
```

This architecture provides a scalable, secure, and multi-tenant billing system suitable for SaaS applications. Would you like me to dive deeper into any specific component?
