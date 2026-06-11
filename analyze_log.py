import csv

def analyze():
    select_pages = []
    insert_pages = []
    
    try:
        with open('log.csv', 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['operation'] == 'SELECT':
                    select_pages.append(int(row['pages_accessed']))
                elif row['operation'] == 'INSERT':
                    insert_pages.append(int(row['io_writes']))
                    
        if select_pages:
            avg_select = sum(select_pages) / len(select_pages)
            print(f"1 Adet SELECT İşlemi İçin Ortalama Sayfa Erişimi: {avg_select:.2f}")
            
        if insert_pages:
            total_writes = sum(insert_pages)
            print(f"5000 INSERT İçin Toplam Disk Yazma (I/O) Sayısı: {total_writes}")
            
    except FileNotFoundError:
        print("log.csv bulunamadı. Önce archive.py'yi çalıştırın.")

if __name__ == "__main__":
    analyze()