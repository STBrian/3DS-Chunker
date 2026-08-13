import sys
import shutil
from pathlib import Path
import re
import json5
import logging
import copy

from typing import Any
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, wait, FIRST_COMPLETED

from anvil import EmptyRegion, Block
import nbtlib
from nbtlib.tag import String
from tqdm import tqdm

from .classes import World, Entry, Chunk, Subfile
from .EmptyChunk import EmptyChunk

OVERWORLD = 0
NETHER = 1
END = 2

AIR = (0, 0)

logger = logging.getLogger(__name__)

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

def notify_log(msg):
    logger.info(msg)
    tqdm.write(f"{msg}")

def notify_err(msg):
    logger.error(msg)
    tqdm.write(f"{RED}{msg}{RESET}")

def notify_warn(msg):
    logger.warning(msg)
    tqdm.write(f"{YELLOW}{msg}{RESET}")

def notify_ok(msg):
    logger.info(msg)
    tqdm.write(f"{GREEN}{msg}{RESET}")

class ChunkConverter:
    def __init__(
        self, position: tuple[int, int, int], entry: Entry, blocks: dict
    ) -> None:
        self.chunk_x, self.chunk_z, self.dimension = position
        self.entry = entry
        self.blocks = blocks
        self.chunk = EmptyChunk(self.chunk_x, self.chunk_z)
        self.ignore_unknown_block_data = False
        self.silent_warnings = False

    def place_blocks(self) -> None:
        setted = set()
        if self.entry.chunk.unknown1 == 3:
            for subchunk_y, subchunk in enumerate(self.entry.data_chunk.data.subchunks):
                y_offset = subchunk_y * 16
                for x, row in enumerate(subchunk.blocks):
                    for z, column in enumerate(row):
                        for y, block in enumerate(column):
                            unknown_block_data = subchunk.unknownBlockData[x][z][y]
                            calculated_y = y + y_offset
                            pos = x, calculated_y, z
                            setted.add(pos)
                            position = x * 16 * 16 + z * 16 + y
                            # extract the nibble from the byte
                            block_byte = subchunk.blockData[position // 2]
                            if position % 2 == 0:
                                block_data = block_byte & 0xF
                            else:
                                block_data = block_byte >> 4

                            block_id = (block, block_data)
                            if unknown_block_data and not self.ignore_unknown_block_data:
                                raise NotImplementedError(
                                    f"Unknown block data 0x{unknown_block_data:02X}"
                                    f" at {(self.chunk_x * 16 + x, calculated_y, self.chunk_z * 16 + z)}"
                                )
                                if block_id == AIR:
                                    block = Block("minecraft", "glass")
                                    self.chunk.set_block(block, x, calculated_y, z)
                                    continue
                            if block_id != AIR:
                                try:
                                    block = self.blocks[block_id]
                                except KeyError:
                                    logger.warning(
                                        f"unknown block {block_id} at {(self.chunk_x * 16 + x, calculated_y, self.chunk_z * 16 + z)} dimension {self.dimension}"
                                    )
                                    # sys.stderr.flush()
                                    # try:
                                    #     block = self.blocks[(block, 0)]
                                    # except:
                                    block = Block("minecraft", "netherite_block")
                                self.chunk.set_block(block, x, calculated_y, z)
        elif self.entry.chunk.unknown1 == 2:
            for x, row in enumerate(self.entry.data_chunk.data.blocks):
                for z, column in enumerate(row):
                    for y, block in enumerate(column):
                        pos = x, y, z
                        setted.add(pos)
                        position = (x * 2048) + (z * 128) + y
                        block_byte = self.entry.data_chunk.data.blockData[position // 2]
                        if position % 2 == 0:
                            block_data = block_byte & 0xF
                        else:
                            block_data = block_byte >> 4

                        block_id = (block, block_data)
                        if block_id != AIR:
                            try:
                                block = self.blocks[block_id]
                            except KeyError:
                                # if not self.silent_warnings:
                                logger.warning(
                                    f"unknown block {block_id} at {(self.chunk_x * 16 + x, y, self.chunk_z * 16 + z)} dimension {self.dimension}"
                                )
                                # sys.stderr.flush()
                                block = Block("minecraft", "netherite_block")
                            self.chunk.set_block(block, x, y, z)
        else:
            raise NotImplementedError(f"Chunk format {self.entry.chunk.unknown1} not implemented")

    def place_biomes(self) -> None:
        if self.entry.chunk.unknown1 == 3:
            for section_z, biome_z in enumerate(self.entry.data_chunk.data.biomes):
                for section_x, biome_v in enumerate(biome_z):
                    self.chunk.paint_biome_column(section_x, section_z, biome_v)
        elif self.entry.chunk.unknown1 == 2:
            for section_z, biome_z in enumerate(self.entry.data_chunk.data.biomes):
                for section_x, biome_v in enumerate(biome_z):
                    self.chunk.paint_biome_column(section_x, section_z, int(biome_v & 0xFF))
        else:
            raise NotImplementedError(f"Chunk format {self.entry.chunk.unknown1} not implemented")

    @property
    def region_position(self) -> tuple[int, int, int]:
        return self.chunk_x // 32, self.chunk_z // 32, self.dimension


class RegionConverter:
    def __init__(self, world_directory: Path, position: tuple[int, int, int]) -> None:
        self.region_x, self.region_z, self.dimension = position
        self.world_directory = Path(world_directory)
        if self.dimension == OVERWORLD:
            dimension_path = self.world_directory
        elif self.dimension == NETHER:
            dimension_path = self.world_directory / "DIM-1"
        elif self.dimension == END:
            dimension_path = self.world_directory / "DIM1"
        else:
            raise ValueError("invalid dimension")
        self.region_file = (
            dimension_path / "region" / f"r.{self.region_x:d}.{self.region_z:d}.mca"
        )
        self.region = EmptyRegion(self.region_x, self.region_z)
        self.chunk_count = 0

    def add_chunk(self, chunk: EmptyChunk) -> None:
        self.region.add_chunk(chunk)

    def save(self) -> None:
        # if the region directory hasn't been generated yet, create it
        if self.world_directory.exists():
            self.region_file.parent.mkdir(parents=True, exist_ok=True)
        self.region.save(str(self.region_file))


def parse_block_json(raw_blocks: dict) -> dict:
    block_json = re.compile(r"^([^\[\]]+)(?:\[([^\[\]]*)\])?$")
    blocks = {}

    for numerical_id, new in raw_blocks["blocks"].items():
        block_str, data_str = numerical_id.split(":")
        block_id = (int(block_str), int(data_str))

        # convert minecraft:item[key=value,otherkey=value] to {"key": "value", "otherkey": "value"}
        parsed = block_json.match(new)
        if parsed is None:
            raise ValueError("invalid block")
        name = parsed[1]
        raw_nbt_data = parsed[2]
        # intentionally not using "is None" because it should match an empty string too
        if raw_nbt_data:
            nbt_data = dict(item.split("=") for item in raw_nbt_data.split(","))
        else:
            nbt_data = {}

        namespace, block = name.split(":")
        blocks[block_id] = Block(namespace, block, nbt_data)

    return blocks

def save_region(region_converter: RegionConverter) -> None:
    region_converter.save()

def deconstruct_chunk_converter(chunk_converter: ChunkConverter) -> tuple:
    return (
        (chunk_converter.chunk_x, chunk_converter.chunk_z, chunk_converter.dimension),
        chunk_converter.entry.chunk._subfile._stream.name,
        chunk_converter.entry.chunk._subfile._offset,
        int(chunk_converter.entry.chunk._subfile._size),
        chunk_converter.blocks,
        chunk_converter.entry.chunk._raw,
        chunk_converter.ignore_unknown_block_data,
        chunk_converter.silent_warnings,
        # str(Path.cwd())
    )

def process_chunk(chunk_converter: ChunkConverter) -> ChunkConverter:
    # dump_to_file(chunk_converter, "test_bad.txt")
    chunk_converter.place_blocks()
    chunk_converter.place_biomes()
    return chunk_converter.chunk

def process_chunk2(data: tuple):
    from io import BytesIO
    from mc3ds.parser import parser

    # debugPath = Path(data[8]) / "debug"
    # if not debugPath.exists() or not debugPath.is_dir():
    #     debugPath.mkdir()

    # reconstruct ChunkConverter
    file = open(data[1], "rb")
    filedata = BytesIO(file.read())
    filedata.seek(data[2])
    file.close()
    subfile = Subfile(filedata, data[3])
    chunk = Chunk(subfile)
    # this is a workaround cause I'm not sure why subfile is not working
    chunk._raw = data[5]
    chunk._header = parser.ChunkHeader(chunk._raw)

    # Always 0? No
    if chunk.unknown0 != 0 or chunk.unknown_parameter_1 != 0:
        notify_log(f"Chunk info: param0={chunk.unknown_parameter_0}, param1={chunk.unknown_parameter_1}, unk0={chunk.unknown0}, unk1={chunk.unknown1}, unk2={chunk.unknown2}")
    # assert chunk.unknown1 == 3 # Seems to be the value that all correct chunks have
    # if chunk.unknown1 != 3 and chunk.unknown1 != 2:
    #     try:
    #         count = 0
    #         for subchunk in chunk[0].data.subchunks:
    #             if subchunk.constant0 != 0:
    #                 with open(debugPath / f"debug_chunk.{chunk.position[0]}_{chunk.position[1]}_{chunk.position[2]}_{count}.txt", mode="w") as f:
    #                     print(f"Format: {subchunk.constant0}", file=f)
    #                     print("Blocks", file=f)
    #                     print(subchunk.blocks, file=f)
    #                     print("Blocks data", file=f)
    #                     print(subchunk.blockData, file=f)
    #                     print("Blocks data 2", file=f)
    #                     print(subchunk.unknownBlockData, file=f)
    #             count += 1
    #     except Exception as e:
    #         print(f"Error! {e}")

    entry = Entry(None, chunk)
    chunk_converter = ChunkConverter(data[0], entry, data[4])
    chunk_converter.ignore_unknown_block_data = data[6]
    chunk_converter.silent_warnings = data[7]
    process_chunk(chunk_converter)
    return chunk_converter.chunk

# @profile
def convert(
    world: World,
    blank_world: Path,
    world_out_str: str,
    delete_out: bool = False,
    interactive: bool = True,
    world_void = False,
    developer: bool = False,
) -> None:
    if world_out_str == "":
        world_out_str = Path.cwd() / re.sub(r'[^\w_. -]', '_', world.name)
    world_out = Path(world_out_str)
    if world_out.exists():
        if not world_out.is_dir() or not (world_out / "level.dat").is_file():
            raise FileExistsError(
                "world output folder already exists, and is not a Java savefile, did you select the right folder?"
            )
        elif delete_out:
            shutil.rmtree(world_out)
        elif interactive:
            print("A converted world already exists, do you want to overwrite it?")
            while True:
                choice = input("[y/n]> ").strip().upper()
                if choice in ("Y", "YES"):
                    shutil.rmtree(world_out)
                    break
                elif choice in ("N", "NO"):
                    print("world output folder already exists", file=sys.stderr)
                    sys.exit(1)
                elif not choice:
                    pass
                else:
                    print("Invalid input, please enter Y or N")
        else:
            print("world output folder already exists", file=sys.stderr)
            sys.exit(1)
    shutil.copytree(blank_world, world_out)
    with nbtlib.load(world_out / "level.dat") as level:
        level["Data"]["LevelName"] = String(world.name)
        # Set world spawn point
        level["Data"]["SpawnX"] = nbtlib.tag.Int(world.metadata.value["SpawnX"])
        # TODO: fix SpawnY
        # level["Data"]["SpawnY"] = nbtlib.tag.Int(world.metadata.value["SpawnY"]) # the 3ds value is weird
        level["Data"]["SpawnZ"] = nbtlib.tag.Int(world.metadata.value["SpawnZ"])
        #level["Data"]["GameRules"] = nbtlib.tag.Compound()
        #level["Data"]["GameRules"]["doMobSpawning"] = nbtlib.tag.String("true")
        if "fml" in level:
            del level["fml"]
        if "forge" in level:
            del level["forge"]
        # Delete Player from level.dat so it uses the world spawn point (at least while no player info is imported)
        if "Player" in level["Data"]:
            del level["Data"]["Player"]
        if "DataPacks" in level["Data"]:
            del level["Data"]["DataPacks"]
        if "GameRules" in level["Data"]:
            del level["Data"]["GameRules"]

        # remove when no void world
        if not world_void:
            if "WorldGenSettings" in level["Data"]:
                del level["Data"]["WorldGenSettings"]

        if "CustomBossEvents" in level["Data"]:
            del level["Data"]["CustomBossEvents"]
        if "ServerBrands" in level["Data"]:
            del level["Data"]["ServerBrands"]
        #if "WasModded" in level["Data"]:
            #del level["Data"]["WasModded"]
        if "ScheduledEvents" in level["Data"]:
            del level["Data"]["ScheduledEvents"]
        #print(level["Data"])

    # read the JSON files containing MCPE block IDs
    with open(Path(__file__).parent / "data" / "blocks.jsonc") as blocks_file:
        raw_blocks = json5.load(blocks_file)
    blocks = parse_block_json(raw_blocks)

    chunk_converters: list[ChunkConverter] = []

    for position, entry in world.entries.items():
        # Create a deep copy of the entry to avoid memory leak. 
        # That's because the entry is stored in world.entries with all the processing data that wont
        # be used after converting. And because the entry is still referenced, it wont be garbage collected.
        chunk_converters.append(ChunkConverter(position, copy.deepcopy(entry), blocks))
        if developer:
            chunk_converters[-1].ignore_unknown_block_data = True
            chunk_converters[-1].silent_warnings = True

    region_converters: dict[Any, RegionConverter] = {}
        
    # get all regions
    for chunk_converter in chunk_converters:
        current_region_position = chunk_converter.region_position
        if not current_region_position in region_converters:
            region_converters[current_region_position] = RegionConverter(world_out, current_region_position)
            region_converters[current_region_position].chunk_count = 1
        else:
            region_converters[current_region_position].chunk_count += 1

    region_executor = ProcessPoolExecutor(max_workers=6)
    useThreadPool = False
    PoolExecutor = ProcessPoolExecutor
    if useThreadPool:
        PoolExecutor = ThreadPoolExecutor

    # process chunks for each region, save it and discard after to save memory
    total_regions = len(region_converters.keys())
    ri = 0
    region_converters_keys = list(region_converters.keys())
    with PoolExecutor(max_workers=8) as chunk_executor:
        for key in region_converters_keys:
            future_metadata = {}
            region_conv = region_converters[key]
            pbar = tqdm(total=region_conv.chunk_count, desc=f"Converting region {ri+1}/{total_regions} chunks")
            count_ok = 0
            count_bad = 0

            pending = set()

            for i in range(len(chunk_converters) - 1, -1, -1):
                chunk_converter = chunk_converters[i]

                if key != chunk_converter.region_position:
                    continue

                if not useThreadPool:
                    future = chunk_executor.submit(
                        process_chunk2,
                        deconstruct_chunk_converter(chunk_converter)
                    )
                else:
                    future = chunk_executor.submit(
                        process_chunk,
                        chunk_converter
                    )

                pending.add((future, i))
                future_metadata[future] = {
                    "position": (chunk_converter.chunk_x, chunk_converter.chunk_z, chunk_converter.dimension)
                }

                # No mandar demasiados chunks simultáneamente
                if len(pending) >= 16:
                    done, _ = wait(
                        [f for f, _ in pending],
                        return_when=FIRST_COMPLETED
                    )

                    for future in done:
                        try:
                            chunk = future.result()
                            region_conv.add_chunk(chunk)
                            count_ok += 1
                        except Exception as e:
                            md = future_metadata.pop(future)
                            pos = md["position"]
                            if not developer:
                                notify_err(f"Failed to convert chunk at: x={pos[0]}, z={pos[1]}, dimension={pos[2]}")
                            else:
                                notify_err(f"Failed to convert chunk at: x={pos[0]}, z={pos[1]}, dimension={pos[2]} -> {e}")
                            count_bad += 1

                        pbar.update()
                        if useThreadPool:
                            for tmp_chunk_conv in chunk_converters:
                                if tmp_chunk_conv.chunk == chunk:
                                    chunk_converters.remove(tmp_chunk_conv)

                        pending = {
                            (f, i)
                            for f, i in pending
                            if f not in done
                        }

            # Esperar los chunks restantes
            for future, i in pending:
                try:
                    chunk = future.result()
                    region_conv.add_chunk(chunk)
                    count_ok += 1
                except:
                    md = future_metadata.pop(future)
                    pos = md["position"]
                    if not developer:
                        notify_err(f"Failed to convert chunk at: x={pos[0]}, z={pos[1]}, dimension={pos[2]}")
                    else:
                        notify_err(f"Failed to convert chunk at: x={pos[0]}, z={pos[1]}, dimension={pos[2]} -> {e}")
                    count_bad += 1

                pbar.update()
                if useThreadPool:
                    for tmp_chunk_conv in chunk_converters:
                        if tmp_chunk_conv.chunk == chunk:
                            chunk_converters.remove(tmp_chunk_conv)

            notify_ok(f"Processed chunks for region {ri+1}/{total_regions}")
            if count_bad > 0:
                notify_warn(f"Success: {count_ok}     Fail: {count_bad}")
            else:
                notify_ok(f"Success: {count_ok}     Fail: {count_bad}")
            # notify_ok(f"Saving region {ri+1}/{total_regions}")
            future = region_executor.submit(save_region, region_converters[key])
            # future_region_md[future]
    
            def done_callback(future, ri, key, region_x, region_z, dimension):
                try:
                    future.result()
                    notify_ok(f"Saved region {region_x}, {region_z} dimension {dimension}")
                except Exception as e:
                    notify_err(f"Error saving region {ri+1}/{total_regions}: {e}")
                del region_converters[key]

            
            future.add_done_callback(lambda f, ri=ri, key=key, x=region_conv.region_x, z=region_conv.region_z, dim=region_conv.dimension: done_callback(f, ri, key, x, z, dim))
            pbar.close()
            ri += 1

    region_executor.shutdown(wait=True)