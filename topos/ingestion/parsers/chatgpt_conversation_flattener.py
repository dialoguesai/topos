"""ChatGPT conversation flattener.

Converts nested conversation objects from ChatGPT export format into flat message records.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger("topos.ingestion.parser.chatgpt_flattener")


def flatten_conversation(conversation: Dict[str, Any], include_system: bool = False) -> Iterator[Dict[str, Any]]:
    """Flatten a ChatGPT conversation object into individual message records.
    
    Args:
        conversation: ChatGPT conversation object with 'mapping' field
        include_system: Whether to include system messages (default: False)
        
    Yields:
        Flattened message records compatible with chatgpt.conversation.v1 format
    """
    mapping = conversation.get("mapping", {})
    if not mapping:
        logger.warning("Conversation has no mapping field")
        return  # Generator function - return without yielding means empty
    
    conv_id = conversation.get("conversation_id") or conversation.get("id", "")
    conv_title = conversation.get("title")
    conv_create_time = conversation.get("create_time")
    
    # Traverse the message tree and extract messages
    visited = set()
    
    def traverse_node(node_id: str) -> Iterator[Dict[str, Any]]:
        """Recursively traverse message nodes and yield records."""
        if node_id in visited or node_id not in mapping:
            return
        
        visited.add(node_id)
        node = mapping[node_id]
        message = node.get("message")
        
        # Skip nodes without messages (root nodes, etc.)
        if not message:
            # Still traverse children
            children = node.get("children")
            if children:
                for child_id in children:
                    yield from traverse_node(child_id)
            return
        
        # Extract message data
        role = message.get("author", {}).get("role", "").lower()
        
        # Skip system messages unless explicitly included
        if role == "system" and not include_system:
            # Still traverse children
            children = node.get("children")
            if children:
                for child_id in children:
                    yield from traverse_node(child_id)
            return
        
        # Extract content
        content_obj = message.get("content", {})
        content_type = content_obj.get("content_type", "text")
        parts = content_obj.get("parts", [])
        
        # Handle different content types
        content = extract_content(content_obj, content_type)
        
        # Skip messages with empty content (unless they're tool calls)
        if not content and content_type == "text":
            # Still traverse children (might be tool execution results)
            children = node.get("children")
            if children:
                for child_id in children:
                    yield from traverse_node(child_id)
            return
        
        # Extract timestamp
        create_time = message.get("create_time")
        if create_time is None:
            create_time = conv_create_time
        
        # Map role to expected format
        # ChatGPT uses: user, assistant, system, tool
        # We need: user -> "user", assistant -> "assistant", tool -> "assistant"
        mapped_role = role
        if role == "tool":
            mapped_role = "assistant"  # Tool messages are from assistant
        
        # Create flattened record
        record = {
            "id": message.get("id", node_id),
            "thread_id": conv_id,
            "role": mapped_role,
            "content": content,
            "created_at": create_time,
            # Additional metadata (optional, for debugging)
            "_metadata": {
                "conversation_title": conv_title,
                "node_id": node_id,
                "parent_id": node.get("parent"),
                "content_type": content_type,
                "original_role": role,
            },
        }
        
        yield record
        
        # Traverse children
        children = node.get("children")
        if children:
            for child_id in children:
                yield from traverse_node(child_id)
    
    # Find root nodes (nodes with no parent or parent not in mapping)
    root_nodes = []
    for node_id, node in mapping.items():
        parent = node.get("parent")
        if not parent or parent not in mapping:
            root_nodes.append(node_id)
    
    # Start traversal from root nodes
    for root_id in root_nodes:
        try:
            yield from traverse_node(root_id)
        except Exception as e:
            logger.warning(f"Error traversing root node {root_id}: {e}", exc_info=True)
            continue


def extract_content(content_obj: Dict[str, Any], content_type: str) -> str:
    """Extract text content from content object based on content type.
    
    Args:
        content_obj: Content object from message
        content_type: Type of content (text, thoughts, reasoning_recap, etc.)
        
    Returns:
        Extracted text content
    """
    if content_type == "text":
        parts = content_obj.get("parts", [])
        if isinstance(parts, list):
            # Join parts, filtering out empty strings
            return " ".join(str(p) for p in parts if p and str(p).strip())
        return str(parts) if parts else ""
    
    elif content_type == "thoughts":
        # Extract thoughts content
        thoughts = content_obj.get("thoughts", [])
        if isinstance(thoughts, list):
            # Extract summary or content from each thought
            thought_texts = []
            for thought in thoughts:
                if isinstance(thought, dict):
                    summary = thought.get("summary", "")
                    content = thought.get("content", "")
                    if summary:
                        thought_texts.append(summary)
                    elif content:
                        thought_texts.append(content)
                elif isinstance(thought, str):
                    thought_texts.append(thought)
            return " ".join(thought_texts)
        return str(thoughts) if thoughts else ""
    
    elif content_type == "reasoning_recap":
        # Extract reasoning recap content
        recap = content_obj.get("reasoning_recap", "")
        if recap:
            return str(recap)
        # Fallback to parts if available
        parts = content_obj.get("parts", [])
        if isinstance(parts, list):
            return " ".join(str(p) for p in parts if p)
        return ""
    
    elif content_type == "code":
        # Extract code content
        code = content_obj.get("code", "")
        if code:
            return f"```\n{code}\n```"
        # Fallback to parts
        parts = content_obj.get("parts", [])
        if isinstance(parts, list):
            return " ".join(str(p) for p in parts if p)
        return ""
    
    elif content_type == "multimodal_text":
        # Extract multimodal text (may have images, etc.)
        parts = content_obj.get("parts", [])
        if isinstance(parts, list):
            # Filter out non-text parts
            text_parts = [str(p) for p in parts if isinstance(p, str) and p.strip()]
            return " ".join(text_parts)
        return ""
    
    elif content_type == "execution_output":
        # Extract execution output
        output = content_obj.get("output", "")
        if output:
            return str(output)
        parts = content_obj.get("parts", [])
        if isinstance(parts, list):
            return " ".join(str(p) for p in parts if p)
        return ""
    
    else:
        # Unknown content type - try to extract parts
        logger.warning(f"Unknown content type: {content_type}")
        parts = content_obj.get("parts", [])
        if isinstance(parts, list):
            return " ".join(str(p) for p in parts if p)
        return ""


def is_conversation_format(record: Dict[str, Any]) -> bool:
    """Check if a record is a ChatGPT conversation object.
    
    Args:
        record: Record to check
        
    Returns:
        True if record appears to be a conversation object
    """
    return (
        isinstance(record, dict) and
        "mapping" in record and
        isinstance(record.get("mapping"), dict) and
        ("conversation_id" in record or "id" in record)
    )


def flatten_conversation_array(conversations: List[Dict[str, Any]], include_system: bool = False) -> Iterator[Dict[str, Any]]:
    """Flatten an array of conversation objects.
    
    Args:
        conversations: List of conversation objects
        include_system: Whether to include system messages
        
    Yields:
        Flattened message records
    """
    if not conversations:
        return
    
    for conv in conversations:
        if not conv or not isinstance(conv, dict):
            logger.warning(f"Skipping invalid conversation object: {type(conv)}")
            continue
            
        if not is_conversation_format(conv):
            logger.warning(f"Skipping non-conversation object: {type(conv)}")
            continue
        
        try:
            for record in flatten_conversation(conv, include_system=include_system):
                yield record
        except Exception as e:
            logger.error(f"Error flattening conversation {conv.get('conversation_id', conv.get('id', 'unknown'))}: {e}", exc_info=True)
            continue
