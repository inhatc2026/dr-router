"""합성 의료기관만 담는 통합 검사 DB. 실사용 DB/외부 API를 사용하지 않는다."""
import sqlite3
from contextlib import closing
from backend.tools import hospital_db


def create_database(path):
    with closing(sqlite3.connect(path)) as con:
        con.executescript(hospital_db.SCHEMA)
        con.execute("INSERT INTO meta VALUES ('schema_version', ?)", (hospital_db.SCHEMA_VERSION,))
        con.executemany("INSERT INTO place (kind,id,name,addr,phone,lat,lng) VALUES (?,?,?,?,?,?,?)", [
            ("병원", "incheon", "테스트 인천 의원", "인천광역시 중구 신포로 1", "032-000-0001", 37.47, 126.62),
            ("병원", "seoul", "테스트 서울 의원", "서울특별시 중구 세종대로 1", "02-000-0001", 37.56, 126.97),
            ("병원", "busan", "테스트 부산 의원", "부산광역시 해운대구 해운대로 1", "051-000-0001", 35.16, 129.16),
            ("약국", "drug", "테스트 인천 약국", "인천광역시 중구 신포로 2", "032-000-0002", 37.471, 126.62),
            ("응급실", "er", "테스트 인천 응급실", "인천광역시 중구 신포로 3", "032-000-0003", 37.472, 126.62),
        ])
        con.commit()


if __name__ == "__main__":
    import sys
    create_database(sys.argv[1])
