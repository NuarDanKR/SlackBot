"""파생 텍스트가 저장소와 PostgreSQL의 문자열 계약을 지키게 한다."""
from __future__ import annotations


def remove_nul(text: str) -> tuple[str, int]:
    """PostgreSQL text가 받을 수 없는 NUL을 제거하고 제거 개수를 돌려준다.

    원본 파일을 고치는 함수가 아니다. 변환 결과와 검색 색인처럼 언제든 다시
    만들 수 있는 파생 텍스트에만 쓴다. PDF 추출기는 글자 사이에 NUL을 섞어
    내보낼 수 있어 U+FFFD를 넣기보다 제거해야 원래 검색어가 이어진다.
    """
    count = text.count("\x00")
    return (text.replace("\x00", "") if count else text, count)
