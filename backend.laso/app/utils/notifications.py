"""
Notification Utilities
Reusable email and SMS notification system
SMS powered by Arkesel (Ghana)
"""
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from enum import Enum
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import httpx
import logging
from pathlib import Path
from jinja2 import Template
import urllib.parse

logger = logging.getLogger(__name__)


class NotificationType(str, Enum):
    """Types of notifications"""
    EMAIL = "email"
    SMS = "sms"
    BOTH = "both"


@dataclass
class EmailConfig:
    """Email server configuration"""
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    from_email: str
    from_name: str = "Pharmacy System"
    use_tls: bool = True
    use_ssl: bool = False


@dataclass
class ArkeselConfig:
    """Arkesel SMS configuration"""
    api_key: str
    sender_id: str = "PHARMACY"
    base_url: str = "https://sms.arkesel.com/api/v2/sms/send"
    timeout: float = 30.0


class EmailNotifier:
    """
    Email notification service
    
    Supports:
    - Plain text emails
    - HTML emails
    - Attachments
    - Template rendering
    
    Example:
        config = EmailConfig(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_user="your-email@gmail.com",
            smtp_password="your-app-password",
            from_email="noreply@pharmacy.com",
            from_name="Pharmacy System"
        )
        
        notifier = EmailNotifier(config)
        
        await notifier.send_email(
            to="customer@example.com",
            subject="Order Confirmation",
            body="Your order has been confirmed!",
            html_body="<h1>Order Confirmed</h1><p>Thank you for your purchase!</p>"
        )
    """
    
    def __init__(self, config: EmailConfig):
        self.config = config
    
    async def send_email(
        self,
        to: str | List[str],
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        attachments: Optional[List[str]] = None,
        reply_to: Optional[str] = None
    ) -> bool:
        """
        Send an email
        
        Args:
            to: Recipient email(s)
            subject: Email subject
            body: Plain text body
            html_body: Optional HTML body
            cc: Carbon copy recipients
            bcc: Blind carbon copy recipients
            attachments: List of file paths to attach
            reply_to: Reply-to email address
        
        Returns:
            True if sent successfully, False otherwise
        """
        try:
            # Create message
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = f"{self.config.from_name} <{self.config.from_email}>"
            
            # Handle multiple recipients
            if isinstance(to, list):
                msg['To'] = ', '.join(to)
            else:
                msg['To'] = to
            
            if cc:
                msg['Cc'] = ', '.join(cc)
            
            if reply_to:
                msg['Reply-To'] = reply_to
            
            # Add body
            msg.attach(MIMEText(body, 'plain'))
            
            if html_body:
                msg.attach(MIMEText(html_body, 'html'))
            
            # Add attachments
            if attachments:
                for filepath in attachments:
                    self._add_attachment(msg, filepath)
            
            # Prepare recipient list
            recipients = [to] if isinstance(to, str) else to
            if cc:
                recipients.extend(cc)
            if bcc:
                recipients.extend(bcc)
            
            # Send email
            if self.config.use_ssl:
                server = smtplib.SMTP_SSL(self.config.smtp_host, self.config.smtp_port)
            else:
                server = smtplib.SMTP(self.config.smtp_host, self.config.smtp_port)
                if self.config.use_tls:
                    server.starttls()
            
            server.login(self.config.smtp_user, self.config.smtp_password)
            server.send_message(msg)
            server.quit()
            
            logger.info(f"Email sent successfully to {to}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send email: {str(e)}")
            return False
    
    def _add_attachment(self, msg: MIMEMultipart, filepath: str):
        """Add file attachment to email"""
        try:
            with open(filepath, 'rb') as f:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f.read())
                encoders.encode_base64(part)
                filename = Path(filepath).name
                part.add_header('Content-Disposition', f'attachment; filename={filename}')
                msg.attach(part)
        except Exception as e:
            logger.error(f"Failed to attach file {filepath}: {str(e)}")
    
    async def send_template_email(
        self,
        to: str | List[str],
        subject: str,
        template_path: str,
        context: Dict[str, Any],
        **kwargs
    ) -> bool:
        """
        Send email using HTML template
        
        Args:
            to: Recipient email(s)
            subject: Email subject
            template_path: Path to Jinja2 HTML template
            context: Template context variables
            **kwargs: Additional arguments for send_email
        
        Returns:
            True if sent successfully
        
        Example:
            await notifier.send_template_email(
                to="user@example.com",
                subject="Welcome!",
                template_path="templates/welcome.html",
                context={
                    "user_name": "John Doe",
                    "activation_link": "https://..."
                }
            )
        """
        try:
            with open(template_path, 'r') as f:
                template = Template(f.read())
            
            html_body = template.render(**context)
            
            # Create plain text version (strip HTML)
            import re
            body = re.sub('<[^<]+?>', '', html_body)
            
            return await self.send_email(
                to=to,
                subject=subject,
                body=body,
                html_body=html_body,
                **kwargs
            )
        except Exception as e:
            logger.error(f"Failed to send template email: {str(e)}")
            return False


class ArkeselSMSNotifier:
    """
    Arkesel SMS notification service for Ghana
    
    Example:
        config = ArkeselConfig(
            api_key="your-arkesel-api-key",
            sender_id="PHARMACY",
            base_url="https://sms.arkesel.com/api/v2/sms/send"
        )
        
        notifier = ArkeselSMSNotifier(config)
        
        # Send single SMS
        result = await notifier.send_sms(
            to="+233501234567",
            message="Your OTP is: 123456"
        )
        
        # Send bulk SMS
        result = await notifier.send_sms(
            to=["+233501234567", "+233509876543"],
            message="Flash sale! 20% off today only."
        )
    """
    
    def __init__(self, config: ArkeselConfig):
        self.config = config
    
    async def send_sms(
        self,
        to: str | List[str],
        message: str
    ) -> Dict[str, Any]:
        """
        Send SMS via Arkesel
        
        Args:
            to: Recipient phone number(s) in international format (+233...)
            message: SMS message text
        
        Returns:
            Dictionary with success status and details
            
        Example:
            result = await notifier.send_sms(
                to="+233501234567",
                message="Your order is ready for pickup"
            )
            
            if result['success']:
                print(f"Sent to {result['total_sent']} recipient(s)")
            else:
                print(f"Failed: {result.get('error')}")
        """
        try:
            # Encode message for URL
            encoded_message = urllib.parse.quote(message)
            
            recipients = [to] if isinstance(to, str) else to
            results = []
            
            for recipient in recipients:
                # Build URL with query parameters (Arkesel v1 style)
                url = (
                    f"{self.config.base_url}?"
                    f"action=send-sms&"
                    f"api_key={self.config.api_key}&"
                    f"to={recipient}&"
                    f"from={self.config.sender_id}&"
                    f"sms={encoded_message}"
                )
                
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, timeout=self.config.timeout)
                    
                    result = {
                        'recipient': recipient,
                        'http_status': response.status_code
                    }
                    
                    if response.status_code == 200:
                        try:
                            api_response = response.json()
                            result['success'] = True
                            result['response'] = api_response
                            logger.info(f"SMS sent via Arkesel to {recipient}")
                        except Exception:
                            # Arkesel might return plain text on success
                            result['success'] = True
                            result['response'] = response.text
                            logger.info(f"SMS sent via Arkesel to {recipient}")
                    else:
                        result['success'] = False
                        result['error'] = f"HTTP {response.status_code}: {response.text}"
                        logger.error(f"Arkesel SMS failed for {recipient}: {result['error']}")
                    
                    results.append(result)
            
            # Return aggregated result
            all_success = all(r.get('success', False) for r in results)
            
            return {
                'success': all_success,
                'provider': 'arkesel',
                'results': results,
                'total_sent': sum(1 for r in results if r.get('success')),
                'total_failed': sum(1 for r in results if not r.get('success'))
            }
            
        except httpx.TimeoutException:
            logger.error("Arkesel SMS timeout")
            return {
                'success': False,
                'error': 'Request timeout',
                'provider': 'arkesel'
            }
        except Exception as e:
            logger.error(f"Arkesel SMS failed: {str(e)}")
            return {
                'success': False,
                'error': str(e),
                'provider': 'arkesel'
            }


class NotificationManager:
    """
    Unified notification manager for email and SMS
    
    Example:
        email_config = EmailConfig(...)
        sms_config = ArkeselConfig(
            api_key="your-arkesel-api-key",
            sender_id="PHARMACY"
        )
        
        manager = NotificationManager(
            email_notifier=EmailNotifier(email_config),
            sms_notifier=ArkeselSMSNotifier(sms_config)
        )
        
        # Send SMS
        result = await manager.send_notification(
            type=NotificationType.SMS,
            to="+233501234567",
            message="Your OTP is 123456"
        )
        
        # Send email
        result = await manager.send_notification(
            type=NotificationType.EMAIL,
            to="user@example.com",
            subject="Welcome",
            message="Welcome to our system!"
        )
        
        # Send both
        result = await manager.send_notification(
            type=NotificationType.BOTH,
            email="user@example.com",
            phone="+233501234567",
            subject="Alert",
            message="Your account was accessed"
        )
    """
    
    def __init__(
        self,
        email_notifier: Optional[EmailNotifier] = None,
        sms_notifier: Optional[ArkeselSMSNotifier] = None
    ):
        self.email_notifier = email_notifier
        self.sms_notifier = sms_notifier
    
    async def send_notification(
        self,
        type: NotificationType,
        message: str,
        to: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        subject: Optional[str] = None,
        html_body: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Send notification via email, SMS, or both
        
        Args:
            type: Type of notification (EMAIL, SMS, or BOTH)
            message: Message text
            to: Email or phone (if type is single)
            email: Email address (if type is BOTH)
            phone: Phone number (if type is BOTH)
            subject: Email subject
            html_body: HTML email body
            **kwargs: Additional arguments
        
        Returns:
            Dictionary with success status for each channel
        """
        results = {}
        
        if type in [NotificationType.EMAIL, NotificationType.BOTH]:
            if not self.email_notifier:
                logger.error("Email notifier not configured")
                results['email'] = {'success': False, 'error': 'Email notifier not configured'}
            else:
                recipient = email or to
                if not recipient:
                    logger.error("No email recipient provided")
                    results['email'] = {'success': False, 'error': 'No recipient'}
                else:
                    success = await self.email_notifier.send_email(
                        to=recipient,
                        subject=subject or "Notification",
                        body=message,
                        html_body=html_body,
                        **kwargs
                    )
                    results['email'] = {'success': success}
        
        if type in [NotificationType.SMS, NotificationType.BOTH]:
            if not self.sms_notifier:
                logger.error("SMS notifier not configured")
                results['sms'] = {'success': False, 'error': 'SMS notifier not configured'}
            else:
                recipient = phone or to
                if not recipient:
                    logger.error("No phone recipient provided")
                    results['sms'] = {'success': False, 'error': 'No recipient'}
                else:
                    results['sms'] = await self.sms_notifier.send_sms(
                        to=recipient,
                        message=message
                    )
        
        return results
    
    async def send_otp(
        self,
        type: NotificationType,
        to: str,
        otp: str,
        expiry_minutes: int = 10
    ) -> Dict[str, Any]:
        """
        Send OTP via email or SMS
        
        Args:
            type: EMAIL or SMS
            to: Recipient (email or phone)
            otp: OTP code
            expiry_minutes: OTP validity period
        
        Returns:
            Result dictionary
        """
        message = f"Your OTP is: {otp}. Valid for {expiry_minutes} minutes."
        subject = "Your OTP Code"
        
        return await self.send_notification(
            type=type,
            to=to,
            subject=subject,
            message=message
        )
    
    async def send_password_reset(
        self,
        email: str,
        reset_link: str,
        user_name: str
    ) -> Dict[str, Any]:
        """Send password reset email"""
        if not self.email_notifier:
            return {'email': {'success': False, 'error': 'Email notifier not configured'}}
        
        subject = "Password Reset Request"
        message = f"""
        Hi {user_name},
        
        You requested to reset your password. Click the link below to reset:
        {reset_link}
        
        This link will expire in 1 hour.
        
        If you didn't request this, please ignore this email.
        """
        
        html_body = f"""
        <h2>Password Reset Request</h2>
        <p>Hi {user_name},</p>
        <p>You requested to reset your password. Click the button below to reset:</p>
        <a href="{reset_link}" style="display:inline-block;padding:10px 20px;background:#007bff;color:white;text-decoration:none;border-radius:5px;">
            Reset Password
        </a>
        <p>This link will expire in 1 hour.</p>
        <p>If you didn't request this, please ignore this email.</p>
        """
        
        success = await self.email_notifier.send_email(
            to=email,
            subject=subject,
            body=message,
            html_body=html_body
        )
        
        return {'email': {'success': success}}


# Singleton instance (configure in your app startup)
_notification_manager: Optional[NotificationManager] = None


def get_notification_manager() -> NotificationManager:
    """Get the global notification manager instance"""
    global _notification_manager
    if _notification_manager is None:
        raise RuntimeError("Notification manager not initialized. Call setup_notifications() first.")
    return _notification_manager


def setup_notifications(
    email_config: Optional[EmailConfig] = None,
    arkesel_config: Optional[ArkeselConfig] = None
):
    """
    Initialize the global notification manager
    
    Call this in your app startup:
        from app.utils.notifications import setup_notifications, EmailConfig, ArkeselConfig
        
        setup_notifications(
            email_config=EmailConfig(
                smtp_host="smtp.gmail.com",
                smtp_port=587,
                smtp_user="your-email@gmail.com",
                smtp_password="your-password",
                from_email="noreply@pharmacy.com"
            ),
            arkesel_config=ArkeselConfig(
                api_key="your-arkesel-api-key",
                sender_id="PHARMACY"
            )
        )
    """
    global _notification_manager
    
    email_notifier = EmailNotifier(email_config) if email_config else None
    sms_notifier = ArkeselSMSNotifier(arkesel_config) if arkesel_config else None
    
    _notification_manager = NotificationManager(email_notifier, sms_notifier)