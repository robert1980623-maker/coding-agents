#!/bin/bash
# Install coding-agents to ~/.local/bin
# Usage: ./install.sh [--uninstall]

set -e

INSTALL_DIR="${HOME}/.local/bin"
PACKAGE_DIR="$(cd "$(dirname "$0")" && pwd)"

uninstall() {
    echo "Uninstalling coding-agents..."
    rm -f "${INSTALL_DIR}/coding-agents"
    echo "✓ Removed ${INSTALL_DIR}/coding-agents"
}

install() {
    echo "Installing coding-agents..."
    
    # Check if uv is available
    if ! command -v uv &> /dev/null; then
        echo "Error: uv is required but not installed."
        echo "Install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
    
    # Create a dedicated venv for coding-agents
    VENV_DIR="${HOME}/.local/share/coding-agents"
    echo "Creating virtual environment at ${VENV_DIR}..."
    uv venv "${VENV_DIR}" --python 3.12
    
    # Install the package into the venv
    echo "Installing coding-agents package..."
    uv pip install "${PACKAGE_DIR}" --python "${VENV_DIR}/bin/python"
    
    # Create install directory
    mkdir -p "${INSTALL_DIR}"
    
    # Create wrapper script
    cat > "${INSTALL_DIR}/coding-agents" << EOF
#!/bin/bash
exec "${VENV_DIR}/bin/coding-agents" "\$@"
EOF
    chmod +x "${INSTALL_DIR}/coding-agents"
    
    # Check if INSTALL_DIR is in PATH
    if [[ ":${PATH}:" != *":${INSTALL_DIR}:"* ]]; then
        echo ""
        echo "⚠️  ${INSTALL_DIR} is not in your PATH."
        echo "Add this to your shell config (~/.bashrc, ~/.zshrc, etc.):"
        echo ""
        echo "  export PATH=\"${INSTALL_DIR}:\$PATH\""
        echo ""
    fi
    
    echo "✓ Installed coding-agents to ${INSTALL_DIR}/coding-agents"
    echo ""
    echo "Test with: coding-agents --help"
}

if [ "$1" = "--uninstall" ] || [ "$1" = "-u" ]; then
    uninstall
else
    install
fi
