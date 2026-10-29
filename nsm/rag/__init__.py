"""문서 근거 챗봇의 바탕(R1: 청크·색인, ADR 0056).

`nsm/ports/`는 동결이라 건드리지 않고, 이 패키지 안에 자체 Protocol(`DocumentSource`, `Embedder`)을 둔다.
R1은 문서를 조각내고 임베딩을 붙여 `doc_chunks` 표에 저장하는 데까지다. 검색(R2)과 화면(R3)은 아직 없다.
"""
