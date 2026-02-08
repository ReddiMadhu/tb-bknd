"""
Test script to verify Azure OpenAI connection.
Run this to test your Azure OpenAI credentials.
"""

import sys
from loguru import logger
from src.config import Config
from src.llm_reasoner import LLMReasoner


def main():
    logger.info("Testing Azure OpenAI Configuration...")
    logger.info("=" * 60)

    # Print configuration
    print(Config.summary())

    # Validate configuration
    try:
        Config.validate()
        logger.success("✓ Configuration validation passed")
    except ValueError as e:
        logger.error(f"✗ Configuration validation failed: {e}")
        return False

    # Test LLM connection
    logger.info("\nTesting Azure OpenAI connection...")
    reasoner = LLMReasoner()

    if reasoner.test_connection():
        logger.success("\n✓ All tests passed! Azure OpenAI is ready to use.")
        return True
    else:
        logger.error("\n✗ Azure OpenAI connection test failed.")
        logger.info("\nPlease check your .env file and ensure:")
        logger.info("  1. AZURE_OPENAI_ENDPOINT is set to your Azure OpenAI endpoint")
        logger.info("  2. AZURE_OPENAI_API_KEY is set to your API key")
        logger.info("  3. AZURE_OPENAI_DEPLOYMENT is set to your deployment name")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
