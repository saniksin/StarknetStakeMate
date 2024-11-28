import os
from data.all_paths import ABI_DIR
from utils.read_json import read_json
from dotenv import load_dotenv

load_dotenv()

STARKNET_RPC_URL = os.getenv("STARKNET_RPC_URL")
if not STARKNET_RPC_URL:
    raise ValueError("STARKNET_RPC_URL is not set in .env")


class Contract:
    def __init__(self, address, abi):
        self.address = address
        self.abi = abi
        self.hex_address = hex(address)
        self.hex_address_2 = '0x0' + hex(address)[:2]
        self.hex_address_3 = '0x0' + hex(address)[:3]


class Contracts:
    L2_STAKING_CONTRACT = Contract(
        address=int("0x00ca1702e64c81d9a07b86bd2c540188d92a2c73cf5cc0e508d949015e7e84a7", 16),
        abi=read_json(ABI_DIR / "l2_staking_contract.json"),
    )
