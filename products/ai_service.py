# products/ai_service.py
"""
Universal AI Service Handler with Vision Support
Supports any AI provider configured in the database
Includes image analysis capabilities for vision-enabled models
Now supports custom prompts per provider
"""
import json
import requests
from typing import Dict, List, Any, Optional
from django.conf import settings

class AIServiceError(Exception):
    """Custom exception for AI service errors"""
    pass


class UniversalAIService:
    """
    Universal AI service that works with any provider
    Provider details (API endpoint, headers, request format) are stored in DB
    Now supports vision capabilities for image analysis
    Supports custom prompts per provider using template variables
    """
    
    # Provider-specific configurations
    PROVIDER_CONFIGS = {
        'openai': {
            'endpoint': 'https://api.openai.com/v1/chat/completions',
            'headers_template': {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer {api_key}'
            },
            'request_builder': 'build_openai_request',
            'response_parser': 'parse_openai_response',
            'supports_vision': True,  # GPT-4o and GPT-4o-mini support vision
            'vision_models': ['gpt-4o', 'gpt-4o-mini', 'gpt-4-vision-preview', 'gpt-4-turbo']
        },
        'anthropic': {
            'endpoint': 'https://api.anthropic.com/v1/messages',
            'headers_template': {
                'Content-Type': 'application/json',
                'x-api-key': '{api_key}',
                'anthropic-version': '2023-06-01'
            },
            'request_builder': 'build_anthropic_request',
            'response_parser': 'parse_anthropic_response',
            'supports_vision': True,  # Claude 3 models support vision
            'vision_models': ['claude-3-opus', 'claude-3-sonnet', 'claude-3-haiku', 'claude-3-5-sonnet']
        },
        'google': {
            'endpoint': 'https://generativelanguage.googleapis.com/v1/models/{model}:generateContent',
            'headers_template': {
                'Content-Type': 'application/json',
            },
            'request_builder': 'build_google_request',
            'response_parser': 'parse_google_response',
            'supports_vision': True,  # Gemini models support vision
            'vision_models': ['gemini-pro-vision', 'gemini-1.5-pro', 'gemini-1.5-flash']
        },
        'azure': {
            'endpoint': '{endpoint}/openai/deployments/{model}/chat/completions?api-version=2024-02-15-preview',
            'headers_template': {
                'Content-Type': 'application/json',
                'api-key': '{api_key}'
            },
            'request_builder': 'build_openai_request',
            'response_parser': 'parse_openai_response',
            'supports_vision': True,
            'vision_models': ['gpt-4o', 'gpt-4o-mini', 'gpt-4-vision']
        }
    }
    
    def __init__(self, provider_config: Dict[str, Any]):
        """
        Initialize with provider configuration from database
        
        Args:
            provider_config: Dictionary containing:
                - service_name: e.g., 'openai', 'anthropic', 'custom'
                - model_name: Model to use
                - config: JSON with api_key, max_tokens, temperature, custom_endpoint, prompt_template
        """
        self.service_name = provider_config.get('service_name', '').lower()
        self.model_name = provider_config.get('model_name')
        self.config = provider_config.get('config', {})
        self.api_key = self.config.get('api_key')
        self.max_tokens = self.config.get('max_tokens', 2000)
        self.temperature = self.config.get('temperature', 0.1)
        self.prompt_template = self.config.get('prompt_template')
        
        # Support custom endpoints from config
        self.custom_endpoint = self.config.get('custom_endpoint')
        
        if not self.api_key:
            raise AIServiceError(f"API key not found in provider config")
    
    def get_provider_config(self) -> Dict[str, Any]:
        """Get provider-specific configuration"""
        # Check if provider is in predefined configs
        if self.service_name in self.PROVIDER_CONFIGS:
            return self.PROVIDER_CONFIGS[self.service_name]
        
        # For custom providers, use generic config from database
        if self.custom_endpoint:
            return {
                'endpoint': self.custom_endpoint,
                'headers_template': self.config.get('headers_template', {
                    'Content-Type': 'application/json',
                    'Authorization': 'Bearer {api_key}'
                }),
                'request_builder': 'build_generic_request',
                'response_parser': 'parse_generic_response',
                'supports_vision': self.config.get('supports_vision', False),
                'vision_models': self.config.get('vision_models', [])
            }
        
        raise AIServiceError(f"Unsupported provider: {self.service_name}. Please configure custom_endpoint.")
    
    def supports_vision(self) -> bool:
        """Check if current model supports vision"""
        provider_config = self.get_provider_config()
        if not provider_config.get('supports_vision', False):
            return False
        
        vision_models = provider_config.get('vision_models', [])
        if not vision_models:
            return True  # If supports_vision=True but no model list, assume all models support it
        
        # Check if current model is in vision models list (case-insensitive partial match)
        model_lower = self.model_name.lower()
        return any(vm.lower() in model_lower for vm in vision_models)
    
    def annotate_product(
        self,
        product_info: Dict[str, Any],
        attributes: List[Dict[str, Any]]
    ) -> Dict[str, str]:
        """
        Annotate a product with all attributes in a single API call
        
        Args:
            product_info: Product details (style_id, description, image_url, etc.)
            attributes: List of attributes to annotate, each with:
                - name: Attribute name
                - description: Attribute description
                - allowed_values: Optional list of allowed values
        
        Returns:
            Dictionary mapping attribute names to their values
        """
        try:
            # Build the prompt
            prompt_text = self._build_prompt(product_info, attributes)
            
            # Get provider config
            provider_config = self.get_provider_config()
            
            # Get image URL
            image_url = product_info.get('image_url')
            has_vision = self.supports_vision()
            
            # Build request
            request_builder = getattr(self, provider_config['request_builder'])
            endpoint, headers, payload = request_builder(
                prompt_text, 
                provider_config,
                image_url=image_url if has_vision else None
            )
            
            # Make API call
            response = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=60
            )
            
            if response.status_code != 200:
                raise AIServiceError(
                    f"API request failed with status {response.status_code}: {response.text}"
                )
            
            # Parse response
            response_parser = getattr(self, provider_config['response_parser'])
            result_text = response_parser(response.json())
            
            # Extract attributes from response
            annotations = self._parse_annotations(result_text, attributes)
            
            return annotations
            
        except requests.RequestException as e:
            raise AIServiceError(f"Network error: {str(e)}")
        except Exception as e:
            raise AIServiceError(f"Annotation error: {str(e)}")
    
    def _build_prompt(
        self,
        product_info: Dict[str, Any],
        attributes: List[Dict[str, Any]]
    ) -> str:
        """
        Build prompt for AI annotation.
        Uses custom prompt_template if available, otherwise uses default.
        """
        # If provider has a custom prompt template, use it
        if self.prompt_template:
            return self._build_custom_prompt(product_info, attributes)
        
        # Otherwise, use the default prompt
        return self._build_default_prompt(product_info, attributes)
    
    def _build_custom_prompt(
        self,
        product_info: Dict[str, Any],
        attributes: List[Dict[str, Any]]
    ) -> str:
        """
        Build prompt using provider's custom template.
        Supports variable substitution with {{variable_name}} syntax.

        AVAILABLE VARIABLES:
        - {{PRODUCT_INFO}}: Style ID, Name, Description, Category, Subcategory
        - {{ATTRIBUTES}}: Attribute list with restrictions
        - {{IMAGE_INFO}}: Image availability status
        - {{STYLE_ID}}: Product style ID
        - {{NAME}}: Product name
        - {{DESCRIPTION}}: Product description
        - {{CATEGORY}}: Product category
        - {{SUBCATEGORY}}: Product subcategory
        """
        template = self.prompt_template
        
        # Build attribute list for template
        attributes_text = self._format_attributes_for_template(attributes)
        
        # Build product info text
        product_text = self._format_product_info_for_template(product_info)
        
        # Get image info
        image_url = product_info.get('image_url')
        has_vision = self.supports_vision()
        image_info = ""
        if image_url:
            if has_vision:
                image_info = "[Visual image provided for analysis]"
            else:
                image_info = f"Image URL: {image_url}"
        
        # Available template variables
        template_vars = {
            '{{PRODUCT_INFO}}': product_text,
            '{{ATTRIBUTES}}': attributes_text,
            '{{IMAGE_INFO}}': image_info,
            '{{STYLE_ID}}': str(product_info.get('style_id', 'N/A')),
            '{{NAME}}': str(product_info.get('name', 'N/A')),
            '{{DESCRIPTION}}': str(product_info.get('description', 'N/A')),
            '{{CATEGORY}}': str(product_info.get('category', 'N/A')),
            '{{SUBCATEGORY}}': str(product_info.get('subcategory', 'N/A')),
        }
        
        # Replace all template variables
        prompt = template
        for var, value in template_vars.items():
            prompt = prompt.replace(var, value)
        
        return prompt
    
    def _format_product_info_for_template(self, product_info: Dict[str, Any]) -> str:
        """Format product information for template substitution"""
        return f"""Style ID: {product_info.get('style_id', 'N/A')}
Name: {product_info.get('name', 'N/A')}
Description: {product_info.get('description', 'N/A')}
Category: {product_info.get('category', 'N/A')}
Subcategory: {product_info.get('subcategory', 'N/A')}"""
    
    def _format_attributes_for_template(self, attributes: List[Dict[str, Any]]) -> str:
        """Format attributes list for template substitution"""
        attributes_with_restrictions = []
        attributes_without_restrictions = []
        
        for idx, attr in enumerate(attributes, 1):
            attr_text = f"\n{idx}. {attr['name']}"
            if attr.get('description'):
                attr_text += f" - {attr['description']}"
            
            if attr.get('allowed_values'):
                attr_text += f"\n   REQUIRED: Choose from: {', '.join(attr['allowed_values'])}"
                attributes_with_restrictions.append(attr_text)
            else:
                attr_text += "\n   FREE-FORM: Provide your best inference"
                attributes_without_restrictions.append(attr_text)
        
        result = ""
        if attributes_with_restrictions:
            result += "\n\nRestricted Attributes (MUST use allowed values):"
            result += "".join(attributes_with_restrictions)
        
        if attributes_without_restrictions:
            result += "\n\nFree-form Attributes (provide your best inference):"
            result += "".join(attributes_without_restrictions)
        
        return result
    
    def _build_default_prompt(
        self,
        product_info: Dict[str, Any],
        attributes: List[Dict[str, Any]]
    ) -> str:
        """Original default prompt logic with enhanced attribute completeness instructions"""
        image_url = product_info.get('image_url')
        has_vision = self.supports_vision()

        product_desc = f"""Product Information:
    - Style ID: {product_info.get('style_id', 'N/A')}
    - Name: {product_info.get('name', 'N/A')}
    - Description: {product_info.get('description', 'N/A')}
    - Category: {product_info.get('category', 'N/A')}
    - Subcategory: {product_info.get('subcategory', 'N/A')}"""

        if image_url:
            if has_vision:
                product_desc += f"\n- Product Image: [Provided as visual input for analysis]"
            else:
                product_desc += f"\n- Product Image URL: {image_url}"
        else:
            product_desc += "\n- Product Image: Not available"

        attributes_with_restrictions = []
        attributes_without_restrictions = []

        for idx, attr in enumerate(attributes, 1):
            attr_text = f"\n{idx}. {attr['name']}"
            if attr.get('description'):
                attr_text += f" - {attr['description']}"

            if attr.get('allowed_values'):
                attr_text += f"\n   REQUIRED: Choose from: {', '.join(attr['allowed_values'])}"
                attributes_with_restrictions.append(attr_text)
            else:
                attr_text += "\n   FREE-FORM: Provide your best inference based on the product details"
                attributes_without_restrictions.append(attr_text)

        attributes_desc = "Attributes to annotate:"
        if attributes_with_restrictions:
            attributes_desc += "\n\nRestricted Attributes (MUST use allowed values):"
            attributes_desc += "".join(attributes_with_restrictions)

        if attributes_without_restrictions:
            attributes_desc += "\n\nFree-form Attributes (provide your best inference):"
            attributes_desc += "".join(attributes_without_restrictions)

        vision_instruction = ""
        if has_vision and image_url:
            vision_instruction = """
    VISUAL ANALYSIS INSTRUCTIONS:
    - Carefully analyze the product image provided
    - Use visual information to determine attributes like color, material, style, pattern, etc.
    - Cross-reference visual details with the text description
    - The image is the PRIMARY source of truth for visual attributes
    """

        # NEW: Add total attribute count for validation
        total_attributes = len(attributes)
        attribute_names_list = [attr['name'] for attr in attributes]

        prompt = f"""{product_desc}

    {attributes_desc}

    Critical Instructions:
    1. Analyze ALL available information: text description AND product image (if provided)
    2. For RESTRICTED attributes: MUST choose ONLY from the allowed values list
    - If none of the allowed values fit, you may provide your own value
    - If you cannot determine any value, use "Unknown"
    3. For FREE-FORM attributes: Provide your best inference - be descriptive and specific
    4. ONLY use "Unknown" if:
    - For restricted attributes: None of the allowed values apply AND you cannot infer a reasonable value
    - For free-form attributes: You genuinely cannot make ANY reasonable inference from text OR image
    5. Avoid "Unknown" whenever possible - make educated inferences based on ALL available data
    6. When image is available: Prioritize visual evidence for appearance-related attributes
    7. **MANDATORY**: You MUST return EXACTLY {total_attributes} attributes in your response
    8. **MANDATORY**: Every attribute from the list above MUST be present in your JSON response
    9. Return ONLY a valid JSON object with attribute names as keys and values as strings
    {vision_instruction}

    ATTRIBUTE CHECKLIST - YOU MUST INCLUDE ALL {total_attributes} ATTRIBUTES:
    {chr(10).join(f'- {name}' for name in attribute_names_list)}

    Example response format:
    {{
    "Color": "Navy Blue",
    "Material": "Cotton Blend",
    "Size": "Large",
    "Pattern": "Solid",
    "Fit": "Regular Fit"
    }}

    CRITICAL REMINDER: Your response MUST contain ALL {total_attributes} attributes listed above. 
    Missing even one attribute is an error. If you cannot determine a value, use "Unknown".

    Respond with JSON only, no additional text:"""

        return prompt
    
    # Request builders for different providers
    
    def build_openai_request(self, prompt: str, provider_config: Dict, image_url: Optional[str] = None) -> tuple:
        """Build OpenAI-compatible request with vision support"""
        endpoint = provider_config['endpoint']
        
        headers = {}
        for key, value in provider_config['headers_template'].items():
            headers[key] = value.format(api_key=self.api_key)
        
        # Build messages with vision support
        messages = [
            {
                "role": "system",
                "content": "You are a product attribute annotation assistant. Analyze product images and descriptions to extract accurate attributes. Always respond with valid JSON only."
            }
        ]
        
        # Build user message content
        if image_url and self.supports_vision():
            # Vision-enabled: send image + text
            user_content = [
                {
                    "type": "text",
                    "text": prompt
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_url,
                        "detail": "high"  # Use high detail for better analysis
                    }
                }
            ]
        else:
            # Text-only
            user_content = prompt
        
        messages.append({
            "role": "user",
            "content": user_content
        })
        
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"}
        }
        
        return endpoint, headers, payload
    
    def build_anthropic_request(self, prompt: str, provider_config: Dict, image_url: Optional[str] = None) -> tuple:
        """Build Anthropic Claude request with vision support"""
        endpoint = provider_config['endpoint']
        
        headers = {}
        for key, value in provider_config['headers_template'].items():
            headers[key] = value.format(api_key=self.api_key)
        
        # Build message content with vision support
        if image_url and self.supports_vision():
            # For Claude, we need to fetch and encode the image
            # Note: Claude accepts images as base64 or URLs via their API
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": image_url
                    }
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        else:
            content = prompt
        
        payload = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ]
        }
        
        return endpoint, headers, payload
    
    def build_google_request(self, prompt: str, provider_config: Dict, image_url: Optional[str] = None) -> tuple:
        """Build Google Gemini request with vision support"""
        endpoint = provider_config['endpoint'].format(model=self.model_name)
        endpoint += f"?key={self.api_key}"
        
        headers = provider_config['headers_template']
        
        # Build parts with vision support
        parts = []
        
        if image_url and self.supports_vision():
            # Add image part
            parts.append({
                "fileData": {
                    "mimeType": "image/jpeg",
                    "fileUri": image_url
                }
            })
        
        # Add text part
        parts.append({"text": prompt})
        
        payload = {
            "contents": [
                {
                    "parts": parts
                }
            ],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
            }
        }
        
        return endpoint, headers, payload
    
    def build_generic_request(self, prompt: str, provider_config: Dict, image_url: Optional[str] = None) -> tuple:
        """Build generic request for custom providers"""
        endpoint = provider_config['endpoint']
        
        headers = {}
        for key, value in provider_config['headers_template'].items():
            headers[key] = value.format(api_key=self.api_key)
        
        # Use config's request format if specified
        if 'request_format' in self.config:
            payload = self.config['request_format']
            payload_str = json.dumps(payload)
            payload_str = payload_str.replace('{prompt}', prompt)
            payload_str = payload_str.replace('{model}', self.model_name)
            payload_str = payload_str.replace('{max_tokens}', str(self.max_tokens))
            payload_str = payload_str.replace('{temperature}', str(self.temperature))
            if image_url:
                payload_str = payload_str.replace('{image_url}', image_url)
            payload = json.loads(payload_str)
        else:
            # Default generic format
            payload = {
                "model": self.model_name,
                "prompt": prompt,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature
            }
            if image_url:
                payload["image_url"] = image_url
        
        return endpoint, headers, payload
    
    # Response parsers
    
    def parse_openai_response(self, response_data: Dict) -> str:
        """Parse OpenAI response"""
        try:
            return response_data['choices'][0]['message']['content']
        except (KeyError, IndexError) as e:
            raise AIServiceError(f"Failed to parse OpenAI response: {e}")
    
    def parse_anthropic_response(self, response_data: Dict) -> str:
        """Parse Anthropic response"""
        try:
            return response_data['content'][0]['text']
        except (KeyError, IndexError) as e:
            raise AIServiceError(f"Failed to parse Anthropic response: {e}")
    
    def parse_google_response(self, response_data: Dict) -> str:
        """Parse Google Gemini response"""
        try:
            return response_data['candidates'][0]['content']['parts'][0]['text']
        except (KeyError, IndexError) as e:
            raise AIServiceError(f"Failed to parse Google response: {e}")
    
    def parse_generic_response(self, response_data: Dict) -> str:
        """Parse generic response"""
        if 'response_path' in self.config:
            path = self.config['response_path'].split('.')
            result = response_data
            for key in path:
                result = result[key]
            return result
        
        if 'text' in response_data:
            return response_data['text']
        if 'result' in response_data:
            return response_data['result']
        if 'output' in response_data:
            return response_data['output']
        
        raise AIServiceError("Could not parse response. Configure 'response_path' in provider config.")
    
    def _parse_annotations(
        self,
        result_text: str,
        attributes: List[Dict[str, Any]]
    ) -> Dict[str, str]:
        """
        Parse AI response and extract annotations.
        Ensures ALL attributes are present, assigning "Unknown" to missing ones.
        """
        try:
            # Handle markdown code blocks
            if '```json' in result_text:
                result_text = result_text.split('```json')[1].split('```')[0]
            elif '```' in result_text:
                result_text = result_text.split('```')[1].split('```')[0]
            
            result_text = result_text.strip()
            
            # Parse JSON
            annotations = json.loads(result_text)
            
            # Validate and normalize
            normalized: Dict[str, str] = {}
            attr_names = {attr['name']: attr for attr in attributes}
            unknown_markers = {'unknown', 'n/a', 'not available', 'cannot determine', ''}

            # Process all returned annotations
            for attr_name, value in annotations.items():
                if attr_name not in attr_names:
                    continue

                attr_info = attr_names[attr_name]
                has_allowed_values = bool(attr_info.get('allowed_values'))
                value_str = str(value).strip()
                value_lower = value_str.lower()
                is_unknown_value = value_lower in unknown_markers

                if has_allowed_values:
                    allowed_lower = {v.lower(): v for v in attr_info['allowed_values']}
                    if value_lower in allowed_lower:
                        # Match from allowed values (case-insensitive)
                        normalized[attr_name] = allowed_lower[value_lower]
                    elif is_unknown_value:
                        # Explicitly marked as unknown
                        normalized[attr_name] = "Unknown"
                    else:
                        # Provider gave custom value (allowed as per instructions)
                        normalized[attr_name] = value_str
                else:
                    if is_unknown_value:
                        normalized[attr_name] = "Unknown"
                    else:
                        normalized[attr_name] = value_str

            # CRITICAL FIX: Check for missing attributes and assign "Unknown"
            missing_attributes = []
            for attr in attributes:
                attr_name = attr['name']
                if attr_name not in normalized:
                    missing_attributes.append(attr_name)
                    normalized[attr_name] = "Unknown"
                    print(f"WARNING: Attribute '{attr_name}' was missing from AI response. Assigned 'Unknown'.")

            # Log if any attributes were missing
            if missing_attributes:
                print(f"AI Response Quality Issue: {len(missing_attributes)} attribute(s) missing from response:")
                for missing_attr in missing_attributes:
                    print(f"  - {missing_attr}")
                print(f"Total expected: {len(attributes)}, Total received: {len(annotations)}, Total normalized: {len(normalized)}")

            return normalized
            
        except json.JSONDecodeError as e:
            raise AIServiceError(f"Failed to parse JSON response: {e}\nResponse: {result_text}")
        except Exception as e:
            raise AIServiceError(f"Failed to parse annotations: {e}")


def get_ai_service(provider_id: int) -> UniversalAIService:
    """
    Factory function to get AI service for a provider
    
    Args:
        provider_id: AIProvider database ID
    
    Returns:
        Configured UniversalAIService instance
    """
    from .models import AIProvider
    
    try:
        provider = AIProvider.objects.get(id=provider_id, is_active=True)
    except AIProvider.DoesNotExist:
        raise AIServiceError(f"AI Provider {provider_id} not found or inactive")
    
    provider_config = {
        'service_name': provider.service_name,
        'model_name': provider.model_name,
        'config': provider.config or {}
    }
    
    return UniversalAIService(provider_config)
