using System;
using System.Collections.Generic;
using UnityEngine;

namespace Game.Combat
{
    public partial class Player : MonoBehaviour
    {
        private int hp;
        private List<string> names;
        public int Health { get; set; }
        public int Twice(int x) => x * 2;

        [Test]
        public void TakesDamage(int amount)
        {
            this.hp -= amount;
            Move();
        }

        public void Awake() { }
        public void Start() { var t = Twice(2); }
        public void Update() { Helper.Tick(); }
        public void OnEnable() { }
        public void OnDisable() { }
        public void OnDestroy() { }

#if UNITY_EDITOR
        public void EditorOnly() { }
#endif

        protected void Move() { }
        public void Orphan() { }

        public Player()
        {
            this.hp = 100;
        }
    }

    public partial class Player : IMove
    {
        public void More() { Move(); }
        public void Go() { }
    }

    public static class Helper
    {
        public static void Tick() { }
        public static void Unused() { }
    }

    public interface IMove
    {
        void Go();
    }
}
