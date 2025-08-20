#!/usr/bin/env python3
"""
IPython Kernel MCP Server

A Model Context Protocol server that connects to an existing IPython kernel
and provides code execution capabilities with persistent state.
"""

from mcp.server.fastmcp import FastMCP
import json
import subprocess
import tempfile
import os
import signal
import platform
import time
from importlib import resources
from pathlib import Path
from jupyter_client import BlockingKernelClient

# Initialize the MCP server
mcp = FastMCP("ipython-kernel")

# Platform detection
IS_WINDOWS = platform.system() == "Windows"

# Global connection state
kernel_client = None
kernel_process = None


def resolve_connection_file(connection_file: str = None) -> str:
    """
    Resolve connection file with priority: param > env var > package default
    
    Args:
        connection_file: Explicit connection file path (highest priority)
        
    Returns:
        Resolved connection file path
    """
    # Priority 1: Explicit parameter
    if connection_file:
        return connection_file
    
    # Priority 2: Environment variable
    env_connection = os.environ.get('IPYTHON_MCP_CONNECTION')
    if env_connection:
        return env_connection
    
    # Priority 3: Package default
    try:
        import ipython_mcp
        return str(resources.files(ipython_mcp) / 'default_connection.json')
    except Exception:
        # Fallback for development/editable installs
        package_dir = Path(__file__).parent
        return str(package_dir / 'default_connection.json')




@mcp.tool()
def start_kernel(connection_file: str = None, dry_run: bool = False) -> str:
    """
    Start a new IPython kernel using a connection file and automatically connect to it.
    
    Args:
        connection_file: Path to connection file to use (optional)
                        If not provided, uses IPYTHON_MCP_CONNECTION env var or package default
        dry_run: If True, only display the command that would be run without executing it
        
    Returns:
        Status message with connection details
    """
    global kernel_process
    
    try:
        # Resolve connection file using priority logic
        resolved_file = resolve_connection_file(connection_file)
        connection_path = Path(resolved_file).expanduser()
        
        if not connection_path.exists():
            return f"❌ Connection file not found: {connection_path}"
        
        # Start IPython kernel in background using the connection file
        import sys
        
        if IS_WINDOWS:
            # Create temporary batch file
            temp_dir = tempfile.mkdtemp(prefix="ipython-mcp-")
            batch_file = os.path.join(temp_dir, "start_kernel.bat")
            
            # Always run in separate window
            batch_content = f'@echo Starting IPython MCP server...\n"{sys.executable}" -m IPython kernel --ConnectionFileMixin.connection_file="{connection_path}" --InteractiveShellApp.extensions="[\'autoreload\']" --InteractiveShellApp.exec_lines="[\'%autoreload 2\']"'
            
            if dry_run:
                return f"Would run Windows batch file with content:\n{batch_content}"
            
            with open(batch_file, 'w') as f:
                f.write(batch_content)
            
            kernel_process = subprocess.Popen(
                [batch_file],
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
        else:
            # Unix: Use standard subprocess approach
            cmd = [
                "ipython", "kernel",
                f"--ConnectionFileMixin.connection_file={connection_path}",
                "--InteractiveShellApp.extensions=['autoreload']",
                "--InteractiveShellApp.exec_lines=['%autoreload 2']"
            ]
            
            if dry_run:
                return f"Would run command: {' '.join(cmd)}"
            
            kernel_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,  # Detach from parent process
            )
        
        # Wait a moment for kernel to start
        time.sleep(1)
        
        # Check if kernel started successfully
        if kernel_process.poll() is not None:
            stdout, stderr = kernel_process.communicate()
            stdout_text = stdout.decode('utf-8') if stdout else ""
            stderr_text = stderr.decode('utf-8') if stderr else ""
            
            # Check if error is due to address already in use (kernel already running)
            if "Address already in use" in stderr_text or "ZMQError" in stderr_text:
                # Try to connect to existing kernel instead
                connect_result = connect_to_kernel(str(connection_path))
                return f"⚠️ Kernel already running, attempting to connect to existing kernel instead\n{connect_result}"
            
            error_details = f"Exit code: {kernel_process.returncode}"
            if stdout_text:
                error_details += f"\nStdout: {stdout_text}"
            if stderr_text:
                error_details += f"\nStderr: {stderr_text}"
            return f"❌ Kernel failed to start\n{error_details}"
        
        # Auto-connect to the kernel
        connect_result = connect_to_kernel(str(connection_path))
        
        return f"✅ Started IPython kernel (PID: {kernel_process.pid})\n📁 Using connection file: {connection_path}\n{connect_result}"
        
    except Exception as e:
        return f"❌ Failed to start kernel: {str(e)}"


@mcp.tool()
def connect_to_kernel(connection_file: str = None) -> str:
    """
    Connect to an existing IPython kernel using its connection file.
    
    Args:
        connection_file: Path to the kernel connection JSON file (optional)
                        If not provided, uses IPYTHON_MCP_CONNECTION env var or package default
        
    Returns:
        Connection status message
    """
    global kernel_client
    
    try:
        # Resolve connection file using priority logic
        resolved_file = resolve_connection_file(connection_file)
        connection_path = Path(resolved_file).expanduser()
        
        if not connection_path.exists():
            return f"❌ Connection file not found: {connection_path}"
        
        # Load connection info
        with open(connection_path, 'r') as f:
            connection_info = json.load(f)
        
        # Close existing client if any
        if kernel_client:
            kernel_client.stop_channels()
        
        # Create new kernel client
        kernel_client = BlockingKernelClient()
        kernel_client.load_connection_info(connection_info)
        kernel_client.start_channels()
        
        # Test the connection
        try:
            kernel_client.wait_for_ready(timeout=5)
        except Exception as e:
            return f"❌ Failed to connect to kernel: {str(e)}"
        
        # Add Windows-specific notice
        windows_notice = ""
        if IS_WINDOWS:
            windows_notice = "\n⚠️ Note: interrupt_kernel() not supported on Windows - use Ctrl+C in kernel window to interrupt execution"
        
        return f"✅ Connected to IPython kernel at {connection_info['ip']}:{connection_info['shell_port']}\n📁 Connection file: {connection_path}\n🔑 Using key: {connection_info['key'][:8]}...{windows_notice}"
        
    except Exception as e:
        return f"❌ Failed to connect: {str(e)}"


@mcp.tool()
def execute_code(code: str) -> str:
    """
    Execute Python code on the connected IPython kernel.
    
    Args:
        code: Python code to execute
        
    Returns:
        Execution results including output, results, and any errors
    """
    global kernel_client
    
    if not kernel_client:
        return "❌ Not connected to kernel. Use connect_to_kernel() first."
    
    try:
        # Execute code using high-level API
        msg_id = kernel_client.execute(code)
        
        # Get the reply
        reply = kernel_client.get_shell_msg(timeout=30)
        
        # Collect output from IOPub
        output_parts = []
        
        while True:
            try:
                msg = kernel_client.get_iopub_msg(timeout=1)
                
                # Only process messages from our execution
                if msg.get('parent_header', {}).get('msg_id') != msg_id:
                    continue
                
                msg_type = msg.get('msg_type')
                content = msg.get('content', {})
                
                if msg_type == 'stream':
                    stream_text = content.get('text', '').strip()
                    if stream_text:
                        output_parts.append(stream_text)
                
                elif msg_type == 'execute_result':
                    result = content.get('data', {}).get('text/plain', '')
                    if result:
                        output_parts.append(result)
                
                elif msg_type == 'error':
                    error_name = content.get('ename', 'Error')
                    error_value = content.get('evalue', '')
                    traceback = content.get('traceback', [])
                    
                    error_msg = f"{error_name}: {error_value}"
                    if traceback:
                        # Clean up ANSI codes from traceback
                        clean_traceback = []
                        for line in traceback:
                            # Simple ANSI code removal (basic)
                            clean_line = line.replace('\x1b[0;31m', '').replace('\x1b[0m', '')
                            clean_line = clean_line.replace('\x1b[1;32m', '').replace('\x1b[0;32m', '')
                            clean_traceback.append(clean_line)
                        error_msg += "\n" + "\n".join(clean_traceback)
                    output_parts.append(f"❌ {error_msg}")
                
                elif msg_type == 'status':
                    if content.get('execution_state') == 'idle':
                        break
                        
            except Exception:
                # Timeout or other error getting messages - execution probably done
                break
        
        # Check if execution was successful
        if reply['content']['status'] == 'error':
            error_info = reply['content']
            error_msg = f"{error_info.get('ename', 'Error')}: {error_info.get('evalue', '')}"
            return f"❌ {error_msg}"
        
        if not output_parts:
            return "✅ Code executed successfully (no output)"
        
        return "\n".join(output_parts)
        
    except Exception as e:
        return f"❌ Execution failed: {str(e)}"


@mcp.tool()
def kernel_status() -> str:
    """
    Get the current kernel connection status.
    
    Returns:
        Status information about the kernel connection
    """
    global kernel_client
    
    if not kernel_client:
        return "❌ Not connected to any kernel"
    
    try:
        # Try to get connection info
        connection_info = kernel_client.get_connection_info()
        return f"✅ Connected to kernel at {connection_info['ip']}:{connection_info['shell_port']}"
    except Exception:
        return "✅ Connected to kernel (connection details unavailable)"


@mcp.tool()
def disconnect_kernel() -> str:
    """
    Disconnect from the current kernel.
    
    Returns:
        Disconnection status message
    """
    global kernel_client
    
    try:
        if kernel_client:
            kernel_client.stop_channels()
            kernel_client = None
        
        return "✅ Disconnected from kernel"
        
    except Exception as e:
        return f"❌ Error during disconnection: {str(e)}"


@mcp.tool()
def shutdown_kernel() -> str:
    """
    Gracefully shutdown the current kernel.
    
    Returns:
        Shutdown status message
    """
    global kernel_client, kernel_process
    
    if not kernel_client:
        return "❌ Not connected to any kernel"
    
    try:
        # Step 1: Try graceful shutdown via Jupyter protocol
        try:
            kernel_client.shutdown()
            disconnect_kernel()
            return "✅ Kernel shutdown gracefully via Jupyter protocol"
        except Exception:
            # Continue to forceful shutdown
            pass
        
        # Step 2: Forceful shutdown via process termination
        if kernel_process and kernel_process.poll() is None:
            try:
                if IS_WINDOWS:
                    # Windows: Use terminate() which sends SIGTERM equivalent
                    kernel_process.terminate()
                    
                    # Wait up to 5 seconds for process to terminate
                    try:
                        kernel_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # Force kill if terminate didn't work
                        kernel_process.kill()
                        kernel_process.wait()
                    
                    disconnect_kernel()
                    return "✅ Kernel shutdown forcefully (Windows terminate/kill)"
                else:
                    # Unix-like: Try SIGTERM first, then SIGKILL
                    kernel_process.terminate()
                    try:
                        kernel_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        kernel_process.kill()
                        kernel_process.wait()
                    
                    disconnect_kernel()
                    return "✅ Kernel shutdown forcefully (Unix SIGTERM/SIGKILL)"
                    
            except Exception as e:
                disconnect_kernel()
                return f"⚠️ Kernel process termination failed: {e}, but connections closed"
        
        # Step 3: Clean up connections even if process shutdown failed
        disconnect_kernel()
        return "✅ Kernel connections closed (process may still be running)"
        
    except Exception as e:
        disconnect_kernel()
        return f"❌ Shutdown failed: {str(e)}, but connections closed"


@mcp.tool()
def interrupt_kernel() -> str:
    """
    Send SIGINT (Ctrl+C) to interrupt the current kernel execution.
    Useful for breaking out of infinite loops or long-running code.
    
    Returns:
        Interrupt status message
    """
    global kernel_client, kernel_process
    
    if not kernel_client:
        return "❌ Not connected to any kernel"
    
    if IS_WINDOWS:
        return "❌ Interrupt not supported on Windows - press Ctrl+C in the kernel window to interrupt execution"
    
    try:
        # Try high-level interrupt first
        try:
            kernel_client.interrupt()
            return "✅ Sent interrupt to kernel via Jupyter protocol"
        except Exception:
            # Fall back to OS-level signal
            pass
        
        # Use OS-level signal (Unix only)
        if kernel_process and kernel_process.poll() is None:
            try:
                kernel_process.send_signal(signal.SIGINT)
                return f"✅ Sent SIGINT to kernel (PID: {kernel_process.pid}) via subprocess"
            except Exception as e:
                return f"❌ OS-level interrupt failed: {str(e)}"
        
        return "❌ No interrupt method available"
        
    except Exception as e:
        return f"❌ Failed to interrupt kernel: {str(e)}"


def main():
    """Main entry point for the MCP server"""
    # Clean shutdown on signals - commented out to keep kernel running
    # signal.signal(signal.SIGTERM, lambda sig, frame: shutdown_kernel())
    # signal.signal(signal.SIGINT, lambda sig, frame: shutdown_kernel())
    mcp.run()


if __name__ == "__main__":
    main()