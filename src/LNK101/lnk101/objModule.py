#!/usr/bin/env python3
#
# HAL-S/FC Object Modules
#   ref. USA-001556/p.143
#        HAL-S/FC SDL Interface Control Document sect.2.3 Object Code
#
# Generic IBM Object Modules
#   ref. IBM-C28-6538-3 - IBM System 360, Linkage Editor (1966-10)
#

from __future__ import annotations

from array import array
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import ClassVar
import sys
import warnings

from .asciiToEbcdic import ebcdicToAscii


# ESD - External Symbol Dictionary
#
# The external symbol dictionary (ESD) contains the external symbols that are
# defined or referred to in the module. An external symbol dictionary entry 
# identifies a symbol and its position within the module. Each entry in the 
# external symbol dictionary is classified as one of the following:

#   1. external name - is a name that can be referred to by any control 
#   section or separately assembled or compiled module. It has a defined value 
#   within the module.
#
#       a. control section name is the symbolic name of a control section. 
#          The external symbol dictionary entry specifies the name, the 
#          assembled origin, and the length of a control section. The defined 
#          value of the symbol is the address of the first byte of the control 
#          section.
#
#       b. entry name - is a name within a control section. The external symbol 
#          dictionary entry specifies the assembled address of the name and 
#          identifies the control section to which it belongs.
#
#       c. blank or named common area - is a control section used to reserve a
#          main storage area (containing no data or instructions) for control
#          sections provided by other modules. The reserved storage areas can 
#          also be used as communication centers within a program. The external 
#          symbol dictionary entry specifies the name and length of the common 
#          area. If it is a blank common area, the name field contains blanks.
#
#       d. private code - is an unnamed control section. The external symbol
#          dictionary entry Specifies the assembled address and assigned length 
#          of the control section.  The name field contains blanks.  Since it 
#          has no name, it cannot be referred to by other control sections.
#
#   2. external reference - is a symbol that is defined as an external name 
#      in another separately assembled or compiled module but is referred to 
#      in the module being processed. The external symbol dictionary entry 
#      specifies the name.
#

class EsdType(IntEnum):
	SD = 0x00
	LD = 0x01
	ER = 0x02
	LR = 0x03
	PC = 0x04


class RldType(IntEnum):
	STD_ADDR = 0b0000
	BRCH_32_BRCH = 0b0001
	DATA_32_BRCH = 0b0100
	BRCH_32_DATA = 0b0010
	DATA_32_DATA = 0b0101


class RldDirection(IntEnum):
	POSITIVE = 0
	NEGATIVE = 1


class SymRefType(IntEnum):
	SPACE = 0b000
	CONTROL = 0b001
	DUMMY = 0b010
	COMMON = 0b011
	INSTRUCTION = 0b100
	CCW = 0b101


class SymDataType(IntEnum):
	CHAR = 0x00
	HEX = 0x04
	BIN = 0x08
	UNUSED = 0x0C
	FIXED_FW = 0x10
	FIXED_HW = 0x14
	FLOAT_SHORT = 0x18
	FLOAT_LONG = 0x1C
	ACON_ZCON = 0x20
	YCON = 0x24
	STORE_PROT_ON = 0x80
	STORE_PROT_OFF = 0x84


@dataclass(frozen=True)
class EsdDataItem:
	image: bytes

	def _u24(self, start: int) -> int:
		return (self.image[start] << 16) | (self.image[start + 1] << 8) | self.image[start + 2]

	@property
	def name(self) -> str:
		return "".join(ebcdicToAscii[b] for b in self.image[0:8])

	@property
	def esdType(self) -> EsdType | int:
		raw = self.image[8]
		try:
			return EsdType(raw)
		except ValueError:
			return raw

	@property
	def addr(self) -> int:
		return self._u24(9)

	@property
	def includedRemote(self) -> int:
		return self.image[12]

	@property
	def _lengthField(self) -> int:
		return self._u24(13)

	@property
	def lengthBytes(self) -> int:
		if self.esdType in (EsdType.SD, EsdType.PC):
			return self._lengthField
		return 0

	@property
	def lengthOnEndCard(self) -> bool:
		return self._lengthField == 0

	@property
	def esdIdentifier(self) -> int:
		return self._lengthField

	@property
	def esdIdentifer(self) -> int:
		return self.esdIdentifier

	def __str__(self) -> str:
		esd_type = self.esdType.name if isinstance(self.esdType, EsdType) else f"0x{self.esdType:02X}"
		return (
			f"name=\"{self.name}\" type={esd_type} addr={self.addr:06X} "
			f"includedRemote={self.includedRemote} lengthBytes={self.lengthBytes} "
			f"lengthOnEndCard={self.lengthOnEndCard} esdIdentifier={self.esdIdentifier}"
		)


@dataclass(frozen=True)
class RldDataItem:
	image: bytes

	def _u16(self, start: int) -> int:
		return (self.image[start] << 8) | self.image[start + 1]

	def _u24(self, start: int) -> int:
		return (self.image[start] << 16) | (self.image[start + 1] << 8) | self.image[start + 2]

	@property
	def relPtr(self) -> int:
		return self._u16(0)

	@property
	def posPtr(self) -> int:
		return self._u16(2)

	@property
	def flagByte(self) -> int:
		return self.image[4]

	@property
	def rldType(self) -> RldType | int:
		raw = (self.flagByte >> 4) & 0x0F
		try:
			return RldType(raw)
		except ValueError:
			return raw

	@property
	def addrConstLenBytes(self) -> int:
		encoded = (self.flagByte >> 2) & 0b11
		if encoded == 0b00:
			return 2
		if encoded == 0b01:
			return 4
		return 0

	@property
	def direction(self) -> RldDirection:
		return RldDirection((self.flagByte >> 1) & 0b1)

	@property
	def nextRLDSameRP(self) -> bool:
		return (self.flagByte & 0b1) != 0

	@property
	def address(self) -> int:
		return self._u24(5)

	def __str__(self) -> str:
		rld_type = self.rldType.name if isinstance(self.rldType, RldType) else f"0x{self.rldType:X}"
		return (
			f"relPtr={self.relPtr:04X} posPtr={self.posPtr:04X} rldType={rld_type} "
			f"addrConstLenBytes={self.addrConstLenBytes} direction={self.direction.name} "
			f"nextRLDSameRP={self.nextRLDSameRP} address={self.address:06X}"
		)


@dataclass(frozen=True)
class SymDataItem:
	raw: bytes
	isDataType: bool
	hasMultiplicity: bool
	hasScaling: bool
	symType: SymRefType | int | None
	hasName: bool
	nameLen: int
	dispFromBase: int
	symName: str
	dataType: SymDataType | int | None
	dataLenMinus1: int | None
	M: int
	S: int | None

	@staticmethod
	def _u24(data: bytes, start: int) -> int:
		return (data[start] << 16) | (data[start + 1] << 8) | data[start + 2]

	@staticmethod
	def _ebcdic(data: bytes) -> str:
		return "".join(ebcdicToAscii[b] for b in data)

	@classmethod
	def parse_from(cls, data: bytes, offset: int) -> tuple["SymDataItem", int]:
		if offset + 4 > len(data):
			raise ValueError("Not enough bytes for SYM item header")

		control = data[offset]
		cursor = offset + 1

		is_data_type = (control & 0x80) != 0
		has_multiplicity = is_data_type and ((control & 0x40) != 0)
		has_scaling = is_data_type and ((control & 0x10) != 0)
		has_name = (control & 0x08) == 0
		name_len = 1 + (control & 0x07)

		disp_from_base = cls._u24(data, cursor)
		cursor += 3

		sym_name = ""
		if has_name:
			if cursor + name_len > len(data):
				raise ValueError("Not enough bytes for SYM item name")
			sym_name = cls._ebcdic(data[cursor:cursor + name_len])
			cursor += name_len

		sym_type: SymRefType | int | None = None
		data_type: SymDataType | int | None = None
		data_len_minus_1: int | None = None

		if is_data_type:
			if cursor >= len(data):
				raise ValueError("Not enough bytes for SYM dataType")
			raw_dtype = data[cursor]
			cursor += 1
			try:
				data_type = SymDataType(raw_dtype)
			except ValueError:
				data_type = raw_dtype

			if data_type in (SymDataType.STORE_PROT_ON, SymDataType.STORE_PROT_OFF):
				data_len_minus_1 = None
			elif data_type in (SymDataType.CHAR, SymDataType.HEX, SymDataType.BIN):
				if cursor + 2 > len(data):
					raise ValueError("Not enough bytes for SYM dataLenMinus1 (2-byte)")
				data_len_minus_1 = (data[cursor] << 8) | data[cursor + 1]
				cursor += 2
			else:
				if cursor >= len(data):
					raise ValueError("Not enough bytes for SYM dataLenMinus1 (1-byte)")
				data_len_minus_1 = data[cursor]
				cursor += 1
		else:
			raw_sym_type = (control >> 4) & 0x07
			try:
				sym_type = SymRefType(raw_sym_type)
			except ValueError:
				sym_type = raw_sym_type

		m_value = 1
		if has_multiplicity:
			if cursor + 3 > len(data):
				raise ValueError("Not enough bytes for SYM multiplicity")
			m_value = cls._u24(data, cursor)
			cursor += 3

		s_value: int | None = None
		if has_scaling:
			if cursor + 2 > len(data):
				raise ValueError("Not enough bytes for SYM scale")
			s_value = (data[cursor] << 8) | data[cursor + 1]
			cursor += 2

		item = cls(
			raw=bytes(data[offset:cursor]),
			isDataType=is_data_type,
			hasMultiplicity=has_multiplicity,
			hasScaling=has_scaling,
			symType=sym_type,
			hasName=has_name,
			nameLen=name_len,
			dispFromBase=disp_from_base,
			symName=sym_name,
			dataType=data_type,
			dataLenMinus1=data_len_minus_1,
			M=m_value,
			S=s_value,
		)
		return item, cursor

	def __str__(self) -> str:
		if self.isDataType:
			dtype = self.dataType.name if isinstance(self.dataType, SymDataType) else str(self.dataType)
			return (
				f"DATA dispFromBase={self.dispFromBase:06X} symName=\"{self.symName}\" "
				f"dataType={dtype} dataLenMinus1={self.dataLenMinus1} "
				f"M={self.M} S={self.S}"
			)
		sym_type = self.symType.name if isinstance(self.symType, SymRefType) else str(self.symType)
		return (
			f"REF type={sym_type} dispFromBase={self.dispFromBase:06X} "
			f"symName=\"{self.symName}\""
		)


@dataclass(frozen=True)
class Record:
	CARD_TYPE: ClassVar[str | None] = None
	image: bytes

	@classmethod
	def from_image(cls, image: bytes) -> "Record":
		record = cls(image=image)
		if not record.valid:
			return record

		for subtype in cls.__subclasses__():
			if subtype.CARD_TYPE == record.cardType:
				return subtype(image=image)

		return record

	@property
	def valid(self) -> bool:
		return len(self.image) > 0 and self.image[0] == 0x02

	@property
	def cardType(self) -> str:
		if len(self.image) < 4:
			return ""
		return "".join(ebcdicToAscii[b] for b in self.image[1:4])

	@property
	def data(self) -> array:
		return array("B", self.image)

	@property
	def raw(self) -> array:
		return array("B", self.image)

	@property
	def rawASCII(self) -> str:
		return "".join(ebcdicToAscii[b] for b in self.image)

	@property
	def title(self) -> str:
		if len(self.image) < 73:
			return ""
		return "".join(ebcdicToAscii[b] for b in self.image[72:80])

	@property
	def itemsData(self) -> array:
		return array("B", self.image[16:72])

	def _u16(self, start: int) -> int:
		return (self.image[start] << 8) | self.image[start + 1]

	def _u24(self, start: int) -> int:
		return (self.image[start] << 16) | (self.image[start + 1] << 8) | self.image[start + 2]

	def _u32(self, start: int) -> int:
		return (
			(self.image[start] << 24)
			| (self.image[start + 1] << 16)
			| (self.image[start + 2] << 8)
			| self.image[start + 3]
		)

	def _ebcdic(self, start: int, end: int) -> str:
		return "".join(ebcdicToAscii[b] for b in self.image[start:end])

	def __str__(self) -> str:
		payload = self.image if not self.valid else self.image[4:72]
		hex_data = "".join(f"{b:02X}" for b in payload)
		separator = " " if self.valid else "x"
		return f"{self.cardType}:{separator}{hex_data} {self.title}"


class SymRecord(Record):
	CARD_TYPE = "SYM"

	@property
	def numBytesData(self) -> int:
		return self._u16(10)

	@property
	def symItems(self) -> tuple[SymDataItem, ...]:
		usable = min(self.numBytesData, len(self.itemsData))
		data = bytes(self.itemsData[:usable])
		items = []
		offset = 0
		while offset < len(data):
			try:
				item, next_offset = SymDataItem.parse_from(data, offset)
			except ValueError:
				break
			if next_offset <= offset:
				break
			items.append(item)
			offset = next_offset
		return tuple(items)

	def __str__(self) -> str:
		lines = [super().__str__()]
		for item in self.symItems:
			lines.append(f"{' ' * 16}{item}")
		return "\n".join(lines)


class EsdRecord(Record):
	CARD_TYPE = "ESD"

	@property
	def numDataBytes(self) -> int:
		return self._u16(10)

	@property
	def firstNotLD(self) -> str:
		return self._ebcdic(14, 16)

	@property
	def esdItems(self) -> tuple[EsdDataItem, ...]:
		usable = min(self.numDataBytes, len(self.itemsData))
		item_bytes = self.itemsData[:usable]
		return tuple(
			EsdDataItem(bytes(item_bytes[offset:offset + 16]))
			for offset in range(0, usable, 16)
			if offset + 16 <= usable
		)

	def __str__(self) -> str:
		lines = [super().__str__()]
		for item in self.esdItems:
			lines.append(f"{' ' * 16}{item}")
		return "\n".join(lines)


class TxtRecord(Record):
	CARD_TYPE = "TXT"

	@property
	def addrFirstData(self) -> int:
		return self._u24(5)

	@property
	def numBytesData(self) -> int:
		return self._u16(10)

	@property
	def esdIdentifier(self) -> str:
		return self._ebcdic(14, 16)

	def __str__(self) -> str:
		lines = [super().__str__()]
		lines.append(
			f"{' ' * 16}addrFirstData={self.addrFirstData:06X} "
			f"numBytesData={self.numBytesData} esdIdentifier=\"{self.esdIdentifier}\""
		)
		return "\n".join(lines)


class RldRecord(Record):
	CARD_TYPE = "RLD"

	@property
	def numBytesData(self) -> int:
		return self._u16(10)

	@property
	def rldItems(self) -> tuple[RldDataItem, ...]:
		usable = min(self.numBytesData, len(self.itemsData))
		item_bytes = self.itemsData[:usable]
		return tuple(
			RldDataItem(bytes(item_bytes[offset:offset + 8]))
			for offset in range(0, usable, 8)
			if offset + 8 <= usable
		)

	def __str__(self) -> str:
		lines = [super().__str__()]
		for item in self.rldItems:
			lines.append(f"{' ' * 16}{item}")
		return "\n".join(lines)


class EndRecord(Record):
	CARD_TYPE = "END"

	@property
	def controlSectionLength(self) -> int:
		return self._u32(28)

	@property
	def symEntryPointName(self) -> str:
		return self._ebcdic(16, 24)

	@property
	def addrOfEntryPoint(self) -> int:
		return self._u24(5)

	@property
	def edsIdEntryPoint(self) -> str:
		return self._ebcdic(14, 16)

	def __str__(self) -> str:
		lines = [super().__str__()]
		lines.append(
			f"{' ' * 16}controlSectionLength={self.controlSectionLength:08X} "
			f"symEntryPointName=\"{self.symEntryPointName}\" "
			f"addrOfEntryPoint={self.addrOfEntryPoint:06X} "
			f"edsIdEntryPoint=\"{self.edsIdEntryPoint}\""
		)
		return "\n".join(lines)


class ObjectFile:
	CARD_SIZE = 80

	def __init__(self, pathname: str | Path):
		self.pathname = Path(pathname)
		blob = self.pathname.read_bytes()

		remainder = len(blob) % self.CARD_SIZE
		if remainder != 0:
			warnings.warn(
				f"Ignoring trailing partial card: {remainder} byte(s) at end of {self.pathname}",
				RuntimeWarning,
			)

		self.records = [
			Record.from_image(blob[offset:offset + self.CARD_SIZE])
			for offset in range(0, len(blob) - remainder, self.CARD_SIZE)
		]

	def __len__(self) -> int:
		return len(self.records)

	def __iter__(self):
		return iter(self.records)

	def __getitem__(self, index: int) -> Record:
		return self.records[index]


obj = None


if __name__ == "__main__":
	if len(sys.argv) != 2:
		print(f"Usage: {Path(sys.argv[0]).name} <object-file>")
		raise SystemExit(2)

	obj = ObjectFile(sys.argv[1])
	for index, record in enumerate(obj):
		print(f"{index:>4d} {record}")
