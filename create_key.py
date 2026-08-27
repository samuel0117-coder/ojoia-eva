#!/usr/bin/env python3
"""Script para crear API keys para los modelos de OjoIA."""
import sys
sys.path.insert(0, '/opt/ojoia/code')
from billing import BillingStore

def main():
    billing = BillingStore.instance()
    
    if len(sys.argv) < 2:
        print("Uso: python3 create_key.py <client_id> [label] [plan]")
        print("Planes: free, dev, pro, enterprise")
        print("\nEjemplo: python3 create_key.py usuario1 'Usuario prueba' free")
        return
    
    client_id = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else ""
    plan = sys.argv[3] if len(sys.argv) > 3 else "free"
    
    result = billing.create_key(client_id, label, plan)
    print(f"✅ API Key creada:")
    print(f"   Client: {result['client_id']}")
    print(f"   Key:    {result['key']}")
    print(f"   Plan:   {result['plan']}")
    print(f"\nUsa esta key en el header:")
    print(f"   Authorization: Bearer {result['key']}")

if __name__ == "__main__":
    main()
