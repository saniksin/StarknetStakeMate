from starknet_py.net.full_node_client import FullNodeClient
from starknet_py.contract import Contract
from starknet_py.net.account.account import Account
from starknet_py.net.models.chains import StarknetChainId
from starknet_py.net.signer.stark_curve_signer import KeyPair
from starknet_py.serialization.errors import InvalidValueException
from starknet_py.net.client_errors import ClientError

from data.contracts import STARKNET_RPC_URL, Contracts
from utils.read_json import read_json
from data.all_paths import ABI_DIR


async def parse_validator_staking_info(validator_address):
    client = FullNodeClient(node_url=STARKNET_RPC_URL)
    
    account = Account(
        client=client,
        address="0x4321",
        key_pair=KeyPair(private_key=654, public_key=321),
        chain=StarknetChainId.MAINNET,
    )

    l2_staking_contract = Contract(
        address=Contracts.L2_STAKING_CONTRACT.address,
        abi=Contracts.L2_STAKING_CONTRACT.abi,
        provider=account
    )
    
    try:
        return await l2_staking_contract.functions["get_staker_info"].call(int(validator_address, 16))
    except (InvalidValueException, ClientError):
        return None
    
async def parse_delegator_staking_info(delegator_address, pool_address):
    client = FullNodeClient(node_url=STARKNET_RPC_URL)
    
    account = Account(
        client=client,
        address="0x4321",
        key_pair=KeyPair(private_key=654, public_key=321),
        chain=StarknetChainId.MAINNET,
    )

    l2_pool_contract = Contract(
        address=int(pool_address, 16),
        abi=read_json(ABI_DIR / "l2_pool_contract.json"),
        provider=account
    )
    
    try:
        return await l2_pool_contract.functions["get_pool_member_info"].call(int(delegator_address, 16))
    except (InvalidValueException, ClientError):
        return None
    