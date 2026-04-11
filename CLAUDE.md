# XIAN Network MCP Server - AI Assistant Guide

## Overview
This MCP server provides comprehensive blockchain functionality for the XIAN Network, enabling wallet management, transaction processing, smart contract interaction, DEX trading, and cryptographic operations. XIAN is the first Python-native Layer 1 blockchain where smart contracts are written in native Python.

## Critical Security Guidelines

### ⚠️ Private Key Handling
- **NEVER log, display, or echo private keys in responses**
- Private keys are 64-character hexadecimal strings
- Always validate private key format before operations
- Treat all private keys as highly sensitive data
- When showing operation results, **never include the private key used**

### 🔐 User Confirmation Required
Before executing any transaction that:
- Sends tokens or currency
- Trades on the DEX
- Requires spending chi (transaction fees)

Always confirm with the user:
1. What operation will be performed
2. The amount and destination
3. Estimated chi costs (if known)
4. That they want to proceed

### 🎯 Best Practices
- Always check balance before sending transactions
- Simulate transactions when possible to estimate chi
- Verify addresses are valid (64-character hex strings)
- Use testnet for learning: `https://testnet.xian.org`
- Store recovery phrases/mnemonics securely offline

## XIAN-Specific Concepts

### Chi (Transaction Fees)
Chi are Xian's equivalent of "gas" on other blockchains. Key differences:
- **68% goes to the contract developer** whose contract is executed
- **30% goes to validators** (split evenly among active validators)
- **1% is permanently burned**, creating deflationary pressure
- Chi are paid in XIAN (the native currency token)
- Fixed and predictable costs unlike dynamic gas markets

When simulating transactions, the result includes chi usage estimates.

### State Variables and Storage
XIAN contracts use two types of state variables:
- **Variable**: Single-value storage (e.g., `total_supply = Variable()`)
- **Hash**: Key-value storage, supports up to 16-dimensional keys (e.g., `balances = Hash()`)

### State Key Format
When reading contract state with `get_state`, use this format:
```
contract_name.variable_name
contract_name.variable_name:key1
contract_name.variable_name:key1:key2:key3
```

Examples:
- `currency.balances:8bf21c7dc3a4ff32996bf56a665e1efe3c9261cc95bbf82552c328585c863829`
- `con_pairs.pairs:ABC-XYZ:reserve0`
- `con_token.metadata:token_symbol`

### Address vs Public Key
In XIAN, these terms are often used interchangeably:
- Both are 64-character hexadecimal strings
- Public keys are derived from private keys
- Addresses receive tokens and are used in contract calls
- The implementation uses "public_key" in responses

## Available Tools Reference

### Wallet Management

#### create_wallet()
Creates a new wallet with random Ed25519 keypair.
- No parameters required
- Returns: `public_key`, `private_key`
- **User must save both securely**

#### create_wallet_from_private_key(private_key)
Restores wallet from existing private key.
- Use when user already has a private key
- Validates key format
- Returns: `public_key`, `private_key`

#### create_hd_wallet()
Creates new HD (Hierarchical Deterministic) wallet with BIP39 mnemonic.
- Generates a 24-word recovery phrase with current xian-tech-py defaults
- Derives both XIAN and Ethereum wallets
- Returns: `mnemonic`, `path`, XIAN keys, ETH keys
- **User must save mnemonic securely offline**

#### create_hd_wallet_from_mnemonic(mnemonic)
Restores HD wallet from 12-word or 24-word mnemonic phrase.
- Use for wallet recovery
- Validates mnemonic format
- Derives same wallets as original

### Balance and Transaction Operations

#### get_balance(address, token_contract="currency")
Checks balance for any address and token.
- `address`: Target address (64-char hex)
- `token_contract`: Default is "currency" (XIAN native token)
- Works with any XSC-0001 compliant token
- Returns: `address`, `token_contract`, `balance`

#### get_token_balances(address, limit=100, offset=0, include_zero=False)
Lists all token balances for an address.
- Uses the indexed token-balance endpoint from current xian-tech-py
- Supports pagination with `limit` and `offset`
- Set `include_zero=True` to include tokens with zero balance
- Returns paginated token metadata and balances for the address

#### send_tokens(private_key, to_address, token_contract, amount)
Sends tokens to another address.
- `amount`: Float value (e.g., 10.5)
- `token_contract`: "currency" or any token contract name
- Returns transaction hash on success
- **Always confirm with user before executing**

#### send_transaction(private_key, contract, function, kwargs)
Generic transaction for any contract function.
- `kwargs`: Dictionary of function parameters (can be string JSON)
- Use for custom contract interactions
- Returns transaction hash and result

#### get_transaction(tx_hash)
Retrieves transaction details by hash.
- Returns full transaction data including status, chi used, result
- Use to verify transaction completion

#### simulate_transaction(address, contract, function, kwargs)
Dry-run transaction without executing on-chain.
- Estimates chi costs
- Validates parameters
- Tests contract function logic
- **Use before expensive operations**

### Smart Contract Operations

#### get_contract(contract_name)
Retrieves and decompiles contract source code.
- Returns Python source code
- Use to understand contract functionality
- Contracts are written in Contracting (Python subset)

#### get_state(state_key)
Reads contract state variables.
- Use format: `contract.variable` or `contract.variable:key1:key2`
- Returns current value
- Works for Variable and Hash storage types

**Common patterns:**
- Token balance: `currency.balances:address`
- Token metadata: `con_token.metadata:token_symbol`
- DEX reserves: `con_pairs.pairs:pair_id:reserve0`

### Token Discovery

#### get_token_contract_by_symbol(token_symbol)
Finds contract addresses for token symbols.
- Case-insensitive symbol search (e.g., "USDT", "XIAN")
- May return multiple contracts if symbol is not unique
- Returns: `token_contracts` array, `count`, optional `message`

**Usage pattern:**
```python
# User: "What's the contract for USDT?"
result = await get_token_contract_by_symbol("USDT")
if result['count'] == 1:
    contract = result['token_contracts'][0]
elif result['count'] > 1:
    # Ask user which one they want
```

#### get_token_data_by_contract(token_contract)
Retrieves token metadata.
- Returns: operator, logo URL, name, symbol, website
- Use to display token information to user

### DEX (Decentralized Exchange) Operations

#### get_dex_price(token_contract, base_contract="currency")
Gets current price for a token pair.
- `base_contract`: Usually "currency" (XIAN)
- Returns: price, reserves, pair_id
- Price is quoted as `token` per 1 `base`

#### buy_on_dex(private_key, buy_token, sell_token, amount, slippage=1.0, deadline_min=1.0)
Buys tokens on the DEX.
- `amount`: Amount of `buy_token` to receive
- `slippage`: Maximum acceptable slippage (default 1%)
- `deadline_min`: Transaction deadline in minutes
- Uses contract `con_dex_noob_wrapper`
- **Confirm trade details with user**

**Workflow:**
1. Check user's balance of sell_token
2. Get current price with `get_dex_price`
3. Calculate approximate cost
4. Confirm with user
5. Execute trade

#### sell_on_dex(private_key, sell_token, buy_token, amount, slippage=1.0, deadline_min=1.0)
Sells tokens on the DEX.
- `amount`: Amount of `sell_token` to sell
- Returns: transaction result
- **Confirm trade details with user**

**Important:** The implementation applies a 0.9999 multiplier to amounts that aren't exactly 8 decimal places to handle rounding issues.

### Cryptographic Operations

#### sign_message(private_key, message)
Signs a message with Ed25519.
- Returns: signature (hex string)
- Use for authentication or proof of ownership

#### verify_signature(address, message, signature)
Verifies message signature.
- Returns: boolean (true if valid)
- Use to authenticate messages

#### encrypt_message(sender_private_key, receiver_public_key, message)
Encrypts message using Curve25519.
- End-to-end encryption between parties
- Returns: encrypted message, both public keys

#### decrypt_message(receiver_private_key, sender_public_key, encrypted_message)
Decrypts received message.
- Works for both sender and receiver
- Returns: decrypted message

## Common Workflows

### Workflow 1: Create Wallet and Check Balance
```
1. create_wallet() or create_hd_wallet()
2. Save credentials securely
3. Fund wallet (user must do this externally)
4. get_balance(address, "currency")
```

### Workflow 2: Send Tokens
```
1. get_balance(address, token_contract)
   - Verify sufficient balance
2. Confirm with user: amount, destination, token type
3. send_tokens(private_key, to_address, token_contract, amount)
4. get_transaction(tx_hash) to verify completion
```

### Workflow 3: Trade on DEX
```
1. get_balance(address, sell_token)
   - Verify sufficient balance
2. get_dex_price(buy_token, sell_token)
   - Show current price to user
3. Calculate approximate cost/proceeds
4. Confirm trade with user
5. buy_on_dex() or sell_on_dex()
6. get_transaction(tx_hash) to verify
```

### Workflow 4: Explore Contract
```
1. get_contract(contract_name)
   - Review source code
2. get_state(contract.variable)
   - Read specific state values
3. simulate_transaction()
   - Test function calls
```

### Workflow 5: Token Discovery
```
1. get_token_contract_by_symbol("SYMBOL")
   - Find contract address
2. get_token_data_by_contract(contract)
   - Get token details
3. get_balance(address, contract)
   - Check user's balance
```

## Implementation Details

### Architecture
- **SDK**: xian-py for blockchain interaction
- **Async Operations**: All tools use async/await
- **Connection Pooling**: XianAsync manages HTTP sessions
- **Error Handling**: Comprehensive try/catch with user-friendly messages

### Error Handling Pattern
All tools follow this pattern:
```python
try:
    # Validate inputs
    if not param.strip():
        return "❌ Error: Parameter required"
    
    # Execute operation
    result = await operation()
    
    # Return formatted result
    return result_dict
except Exception as e:
    logger.error(f"Operation failed: {e}")
    return f"❌ Error: {str(e)}"
```

### Async Context Management
```python
async with XianAsync(NODE_URL, wallet=wallet) as xian:
    # Operations here automatically clean up
    result = await xian.send_tx(...)
```

### Network Configuration
Default values (can be configured via environment variables):
- **NODE_URL**: `https://node.xian.org`
- **CHAIN_ID**: `xian-1`
- **GRAPHQL**: `https://node.xian.org/graphql`

Testnet:
- **NODE_URL**: `https://testnet.xian.org`
- **CHAIN_ID**: `xian-testnet-12`

## Response Formatting

### Success Responses
Return structured dictionaries with relevant data:
```python
{
    "public_key": "...",
    "balance": 100.5,
    "tx_hash": "..."
}
```

### Error Responses
Return strings with ❌ emoji and clear error message:
```
"❌ Error: Insufficient balance"
```

### User-Facing Language
- Use clear, non-technical language when possible
- Explain what happened and what to do next
- For errors, suggest solutions
- Don't expose internal implementation details

## Testing and Debugging

### Test Before Real Operations
1. Use testnet for experimentation
2. Simulate transactions before executing
3. Start with small amounts
4. Verify balance after operations

### Common Issues and Solutions

**"Invalid private key"**
- Ensure 64-character hexadecimal string
- No spaces or special characters
- Check if user copied correctly

**"Insufficient balance"**
- Check balance before transactions
- Account for chi costs
- Verify correct token contract

**"Contract not found"**
- Verify contract name spelling
- Check if contract is deployed
- Try on block explorer

**"Transaction failed"**
- Check transaction details with get_transaction
- Review chi used vs. provided
- Verify function parameters

### Enable Debug Logging
If troubleshooting, server logs go to stderr:
```
2025-01-15 10:30:45 - xian-server - INFO - Creating new wallet
```

## Best Practices for AI Assistants

### Do:
- ✅ Always validate inputs before operations
- ✅ Check balances before sending
- ✅ Confirm transactions with users
- ✅ Simulate when possible
- ✅ Explain what you're doing
- ✅ Handle errors gracefully
- ✅ Suggest next steps

### Don't:
- ❌ Never display private keys in responses
- ❌ Don't execute transactions without confirmation
- ❌ Don't assume user wants to proceed
- ❌ Don't ignore error messages
- ❌ Don't make transactions on mainnet without warning

### Communication Style
- Be conversational but precise
- Explain blockchain concepts when relevant
- Warn about irreversible operations
- Celebrate successful transactions
- Be helpful when things fail

## Python Smart Contracts

XIAN uses Contracting, a Python subset for smart contracts:

### Key Features
- **Native Python syntax** - no new language to learn
- **Decorators**: `@construct`, `@export`
- **Built-in storage**: `Variable()`, `Hash()`
- **Context access**: `ctx.caller`, `ctx.this`, `ctx.signer`
- **Standard library**: random, crypto, decimal, hashlib, datetime

### Example Contract
```python
import currency

balances = Hash(default_value=0)

@export
def transfer(amount: float, to: str):
    assert balances[ctx.caller] >= amount, 'Insufficient balance'
    balances[ctx.caller] -= amount
    balances[to] += amount
```

### State Variable Types
- `Variable()`: Single value (e.g., total_supply)
- `Hash()`: Key-value store with up to 16-dimensional keys
- `ForeignHash()`: Read-only view of another contract's Hash
- `ForeignVariable()`: Read-only view of another contract's Variable

## Resources

### Official Documentation
- [XIAN Network Docs](https://docs.xian.org)
- [Contracting Docs](http://contracting.xian.org)
- [Smart Contracts Guide](https://github.com/xian-network/smart-contracts-docs)

### Developer Resources
- [xian-tech-py SDK](https://github.com/xian-network/xian-py)
- [xian-core](https://github.com/xian-network/xian-core)
- [xian-contracting](https://github.com/xian-network/xian-contracting)

### Network Resources
- [Block Explorer](https://xian.org/explorer)
- [Testnet Faucet](https://faucet.xian.org)
- [DEX Interface](https://xian.org/dex)

## Token Standards

### XSC-0001 (Token Standard)
Required methods:
- `transfer(amount: float, to: str)`
- `approve(amount: float, to: str)`
- `transfer_from(amount: float, to: str, main_account: str)`
- `balances` Hash for storing balances

Required metadata:
- `token_name`
- `token_symbol`

Optional features:
- EIP-2612 (permits) support
- ERC-1363 (streaming payments) support

## Economic Model

### Developer Incentives
- **68% of all chi** go to contract developers
- Developers earn automatically on every contract use
- No applications or approval needed
- First-class economic participants

### Supply Dynamics
- Genesis supply: 111,111,111 XIAN
- 1% of chi permanently burned each transaction
- Deflationary pressure over time
- 100% of chi redistributed (68% dev, 30% validators, 1% burn)

This unique model makes XIAN attractive for developers while maintaining network security through validator rewards.
